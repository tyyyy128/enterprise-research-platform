"""Opt-in PostgreSQL recovery after a prior executor exited mid-run.

Run only against a backed-up, migrated development database.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.application import ResearchRunApplicationService, ResearchRunExecutor, ResearchStageOutcome
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
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


class BlockingRunner:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run_stage(self, task_id, run_id, stage, *, lease=None):
        assert lease is not None
        self.calls.append(stage)
        if stage == ResearchRunStatus.SEARCHING:
            self.entered.set()
            await self.release.wait()


async def wait_idle(executor):
    async def poll():
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=5)


def test_postgres_explicit_resume_reuses_plan_and_blocks_competing_worker():
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregate = ResearchAggregate.create("PostgreSQL safe plan handoff")
        run = ResearchRun.create(aggregate.task.task_id)
        try:
            repository = PostgresResearchRepository(sessions)
            runs_repository = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            documents = PostgresRetrievedDocumentRepository(sessions)
            runs = ResearchRunApplicationService(repository, runs_repository)
            await repository.save(aggregate)
            await runs_repository.save(run)
            old = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            plan = ResearchPlan(
                task_id=run.task_id,
                run_id=run.run_id,
                objective="Inspect durable outputs",
                questions=[ResearchQuestion(question="What happened?")],
            )
            await plans.save_guarded(plan, old, run_repository=runs_repository)
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old, outcome=ResearchStageOutcome.COMPLETED
            )
            assert await runs.release_run_lease(old)

            # Fresh repository/service objects approximate a process restart.
            again = PostgresResearchRunRepository(sessions)
            new_app = ResearchRunApplicationService(PostgresResearchRepository(sessions), again)
            inspector = ResearchSafeResumeInspector(
                new_app,
                PostgresResearchRepository(sessions),
                PostgresResearchPlanRepository(sessions),
                documents,
            )
            decision = await inspector.inspect(run.task_id, run.run_id)
            assert decision.disposition == ResearchResumeDisposition.READY
            assert decision.next_stage == ResearchRunStatus.SEARCHING
            left_runner = BlockingRunner()
            right_runner = BlockingRunner()
            left = ResearchRunExecutor(new_app, left_runner, resume_inspector=inspector)
            right = ResearchRunExecutor(new_app, right_runner, resume_inspector=inspector)
            try:
                results = await asyncio.gather(
                    left.resume(run.task_id, run.run_id), right.resume(run.task_id, run.run_id)
                )
                assert sorted(results) == [False, True]
                winner = left if results[0] else right
                winning_runner = left_runner if results[0] else right_runner
                await asyncio.wait_for(winning_runner.entered.wait(), timeout=3)
                assert left_runner.calls + right_runner.calls == [ResearchRunStatus.SEARCHING]
                with pytest.raises(ResearchRunLeaseLostError):
                    await runs.finish_stage(
                        run.task_id,
                        run.run_id,
                        ResearchRunStatus.PLANNING,
                        lease=old,
                        outcome=ResearchStageOutcome.COMPLETED,
                    )
                winning_runner.release.set()
                await wait_idle(winner)
                assert (await again.get(run.run_id)).status == ResearchRunStatus.FAILED
                assert "WRITING proof missing" in ((await again.get(run.run_id)).error or "")
                # Recovery itself succeeded, but the no-op writer has no
                # durable 7C report and cannot finish the overall Run.
                attempts = await again.list_stage_attempts(run.run_id)
                assert len([attempt for attempt in attempts if attempt.stage == ResearchRunStatus.PLANNING]) == 1
            finally:
                left_runner.release.set()
                right_runner.release.set()
                await left.shutdown()
                await right.shutdown()
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_completed_journal_without_plan_cannot_be_recovered():
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregate = ResearchAggregate.create("In doubt no durable plan")
        run = ResearchRun.create(aggregate.task.task_id)
        try:
            repository = PostgresResearchRepository(sessions)
            runs_repository = PostgresResearchRunRepository(sessions)
            runs = ResearchRunApplicationService(repository, runs_repository)
            await repository.save(aggregate)
            await runs_repository.save(run)
            old = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old, outcome=ResearchStageOutcome.COMPLETED
            )
            assert await runs.release_run_lease(old)
            inspector = ResearchSafeResumeInspector(
                runs,
                repository,
                PostgresResearchPlanRepository(sessions),
                PostgresRetrievedDocumentRepository(sessions),
            )
            assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.PAUSED
            executor = ResearchRunExecutor(runs, BlockingRunner(), resume_inspector=inspector)
            assert not await executor.resume(run.task_id, run.run_id)
            assert (await runs_repository.get(run.run_id)).status == ResearchRunStatus.PLANNING
            assert await runs_repository.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
            await executor.shutdown()
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())
