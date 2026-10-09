"""4B: authoritative PostgreSQL Journal/Plan vs simulated Graph snapshot timing.

Requires a migrated, isolated development database. No Graph saver is
installed here: this tests the read-only consistency decision against real
Run, Stage Journal and Plan transactions, not cross-store atomic commits.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.application import (
    ResearchCheckpointObservation,
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointReference,
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
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1",
    reason="Requires a migrated local PostgreSQL development database.",
)


class SimulatedCheckpointReader:
    def __init__(self):
        self.mode = "present"

    async def latest(self, binding):
        if self.mode == "absent":
            return None
        if self.mode == "unavailable":
            raise ConnectionError("Simulated independent checkpoint store outage")
        if self.mode == "wrong_generation":
            binding = ResearchGraphCheckpointBinding(
                binding.task_id, binding.run_id, binding.stage, binding.fencing_token + 1
            )
        return ResearchGraphCheckpointReference(binding, "graph-state-only")


def test_postgres_stage_checkpoint_commit_order_windows_fail_closed():
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregate = ResearchAggregate.create("Postgres 4B dual-store failure windows")
        run = ResearchRun.create(aggregate.task.task_id)
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
            await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
            simulated_graph = SimulatedCheckpointReader()
            inspector = ResearchSafeResumeInspector(runs, research, plans, docs, graph_checkpoints=simulated_graph)

            # Graph commit ahead of Journal and business Plan: cannot skip.
            before_plan = await inspector.inspect(run.task_id, run.run_id)
            assert before_plan.disposition == ResearchResumeDisposition.PAUSED
            assert before_plan.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.PRESENT

            await plans.save_guarded(
                ResearchPlan(
                    task_id=run.task_id,
                    run_id=run.run_id,
                    objective="Commit order analysis",
                    questions=[ResearchQuestion(question="Was the plan committed?")],
                ),
                lease,
                run_repository=repository,
            )
            assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.PAUSED

            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=lease,
                outcome=ResearchStageOutcome.COMPLETED,
            )
            with_checkpoint = await inspector.inspect(run.task_id, run.run_id)
            assert with_checkpoint.disposition == ResearchResumeDisposition.READY
            assert with_checkpoint.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.PRESENT

            # Business commit ahead of Graph checkpoint: only journal + Plan can grant readiness.
            simulated_graph.mode = "absent"
            without_checkpoint = await inspector.inspect(run.task_id, run.run_id)
            assert without_checkpoint.disposition == ResearchResumeDisposition.READY
            assert without_checkpoint.proof_digest != with_checkpoint.proof_digest

            simulated_graph.mode = "unavailable"
            unavailable = await inspector.inspect(run.task_id, run.run_id)
            assert unavailable.disposition == ResearchResumeDisposition.PAUSED
            assert unavailable.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.UNAVAILABLE

            simulated_graph.mode = "wrong_generation"
            conflict = await inspector.inspect(run.task_id, run.run_id)
            assert conflict.disposition == ResearchResumeDisposition.PAUSED
            assert conflict.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.IDENTITY_CONFLICT

            assert await runs.release_run_lease(lease)

            class ForbiddenRunner:
                async def run_stage(self, *args, **kwargs):
                    raise AssertionError("Checkpoint mismatch must not dispatch any external work")

            executor = ResearchRunExecutor(runs, ForbiddenRunner(), resume_inspector=inspector)
            assert not await executor.resume(run.task_id, run.run_id)
            assert executor.active_count == 0
            assert (await repository.get(run.run_id)).status == ResearchRunStatus.PLANNING
            await executor.shutdown()
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())
