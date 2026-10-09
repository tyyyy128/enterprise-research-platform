"""4C: REAL PostgreSQL Checkpointer + journal/artifact + explicit Safe Resume.

Opt-in only. Run against a migrated development business database and a
separate disposable checkpoint database. Neither database is provisioned here.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.application import (
    LangGraphCheckpointReader,
    ResearchGraphCheckpointBinding,
    ResearchLangGraphStageRunner,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchStageOutcome,
)
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.document_repository import PostgresRetrievedDocumentRepository
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
    or not os.getenv("ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN"),
    reason="Requires an isolated migrated PostgreSQL test DB and explicit Checkpointer test DSN.",
)


def test_real_postgres_checkpoint_journal_and_safe_resume():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async def exercise():
        dsn = os.environ["ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN"]
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregate = ResearchAggregate.create("4C real PostgreSQL graph/journal recovery")
        run = ResearchRun.create(aggregate.task.task_id)
        binding = None
        created_threads: set[str] = set()

        class TrackingStageRunner(ResearchLangGraphStageRunner):
            async def run_stage(self, task_id, run_id, stage, *, lease=None):
                if lease is not None:
                    created_threads.add(ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease).thread_id)
                await super().run_stage(task_id, run_id, stage, lease=lease)

        try:
            research = PostgresResearchRepository(sessions)
            repository = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            docs = PostgresRetrievedDocumentRepository(sessions)
            runs = ResearchRunApplicationService(research, repository)
            await research.save(aggregate)
            await repository.save(run)
            lease = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
            assert lease is not None
            stage = ResearchRunStatus.PLANNING
            binding = ResearchGraphCheckpointBinding.for_lease(run.task_id, stage, lease)
            await runs.advance_run(run.task_id, run.run_id, stage, lease=lease)
            await runs.start_stage(run.task_id, run.run_id, stage, lease=lease)

            class PlanningStage:
                def __init__(self):
                    self.calls = 0

                async def run_stage(self, task_id, run_id, stage, *, lease=None):
                    assert (task_id, run_id, stage) == (run.task_id, run.run_id, ResearchRunStatus.PLANNING)
                    self.calls += 1
                    await plans.save_guarded(
                        ResearchPlan(
                            task_id=run.task_id,
                            run_id=run.run_id,
                            objective="Prove business vs Graph state",
                            questions=[ResearchQuestion(question="What did the stage write?")],
                        ),
                        lease,
                        run_repository=repository,
                    )

            planning = PlanningStage()
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                await saver.setup()
                graph_runner = TrackingStageRunner(planning, saver)
                reader = LangGraphCheckpointReader(saver)
                inspector = ResearchSafeResumeInspector(runs, research, plans, docs, graph_checkpoints=reader)

                await graph_runner.run_stage(run.task_id, run.run_id, stage, lease=lease)
                assert planning.calls == 1
                assert await reader.latest(binding) is not None
                before_finish = await inspector.inspect(run.task_id, run.run_id)
                assert before_finish.disposition == ResearchResumeDisposition.PAUSED

                # No second business call when a Graph checkpoint already exists.
                from enterprise_research.application.graph_checkpoint import ResearchGraphCheckpointConflictError

                with pytest.raises(ResearchGraphCheckpointConflictError):
                    await graph_runner.run_stage(run.task_id, run.run_id, stage, lease=lease)
                assert planning.calls == 1

                await runs.finish_stage(
                    run.task_id, run.run_id, stage, lease=lease, outcome=ResearchStageOutcome.COMPLETED
                )
                after_finish = await inspector.inspect(run.task_id, run.run_id)
                assert after_finish.disposition == ResearchResumeDisposition.READY
                assert await runs.release_run_lease(lease)

            # Distinct async PostgreSQL connections simulate a service restart.
            async with AsyncPostgresSaver.from_conn_string(dsn) as restarted_saver:
                restarted_reader = LangGraphCheckpointReader(restarted_saver)
                assert await restarted_reader.latest(binding) is not None
                restarted_inspector = ResearchSafeResumeInspector(
                    runs, research, plans, docs, graph_checkpoints=restarted_reader
                )

                class LaterStage:
                    def __init__(self):
                        self.calls = []

                    async def run_stage(self, task_id, run_id, stage, *, lease=None):
                        self.calls.append(stage)

                later = LaterStage()
                executor = ResearchRunExecutor(
                    runs, TrackingStageRunner(later, restarted_saver), resume_inspector=restarted_inspector
                )
                try:
                    assert await executor.resume(run.task_id, run.run_id)
                    for _ in range(300):
                        if executor.active_count == 0:
                            break
                        await asyncio.sleep(0.01)
                    assert executor.active_count == 0
                    persisted = await repository.get(run.run_id)
                    assert persisted is not None and persisted.status == ResearchRunStatus.COMPLETED
                    assert planning.calls == 1
                    assert ResearchRunStatus.SEARCHING in later.calls
                    assert ResearchRunStatus.PLANNING not in later.calls
                finally:
                    await executor.shutdown()
        finally:
            # Each stage/generation has its own root-Graph thread (v2).
            # Remove every test-owned thread, never unrelated host checkpoints.
            if created_threads:
                async with AsyncPostgresSaver.from_conn_string(dsn) as cleanup_saver:
                    for thread_id in sorted(created_threads):
                        await cleanup_saver.adelete_thread(thread_id)
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())
