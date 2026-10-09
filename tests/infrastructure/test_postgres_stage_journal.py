"""Real PostgreSQL stage journal tests. Use a backed-up, migrated dev database."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, literal_column, select, update

from enterprise_research.application import (
    ResearchRunLeaseLostError,
    ResearchStageJournalConflictError,
    ResearchStageOutcome,
)
from enterprise_research.domain import ResearchAggregate, ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.models import ResearchRunRow

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1",
    reason="Set ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 with a migrated local PostgreSQL database.",
)


def test_postgres_journal_persists_across_adapter_restart_and_rejects_duplicates() -> None:
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        repo = PostgresResearchRunRepository(sessions)
        task = ResearchAggregate.create("Durable stage journal")
        run = ResearchRun.create(task.task.task_id)
        try:
            await aggregates.save(task)
            await repo.save(run)
            lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert lease is not None
            run.advance(ResearchRunStatus.PLANNING)
            await repo.save_guarded(run, lease, expected_status=ResearchRunStatus.PENDING)
            with pytest.raises(ResearchStageJournalConflictError):
                await repo.finish_stage(lease, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
            started = await repo.start_stage(lease, ResearchRunStatus.PLANNING)
            assert started.outcome == ResearchStageOutcome.STARTED
            assert started.finished_at is None
            with pytest.raises(ResearchStageJournalConflictError):
                await repo.start_stage(lease, ResearchRunStatus.PLANNING)
            reopened = PostgresResearchRunRepository(sessions)
            assert await reopened.list_stage_attempts(run.run_id) == [started]
            finished = await reopened.finish_stage(
                lease, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED
            )
            assert finished.started_at == started.started_at
            assert finished.finished_at >= started.started_at
            assert finished.error is None
            with pytest.raises(ResearchStageJournalConflictError):
                await repo.finish_stage(
                    lease, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.FAILED, error="too late"
                )
            assert await repo.list_stage_attempts(run.run_id) == [finished]
            assert (await reopened.get(run.run_id)).status == ResearchRunStatus.PLANNING
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_journal_rejects_stale_worker_after_explicit_takeover() -> None:
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        left = PostgresResearchRunRepository(sessions)
        right = PostgresResearchRunRepository(sessions)
        task = ResearchAggregate.create("Fenced stage attempts")
        run = ResearchRun.create(task.task.task_id)
        try:
            await aggregates.save(task)
            await left.save(run)
            first = await left.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert first is not None
            run.advance(ResearchRunStatus.PLANNING)
            await left.save_guarded(run, first, expected_status=ResearchRunStatus.PENDING)
            await left.start_stage(first, ResearchRunStatus.PLANNING)
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchRunRow)
                        .where(ResearchRunRow.id == run.run_id)
                        .values(lease_expires_at=func.clock_timestamp() - literal_column("INTERVAL '1 second'"))
                    )
            with pytest.raises(ResearchRunLeaseLostError):
                await left.finish_stage(first, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
            assert await right.claim_lease(run.run_id, uuid4(), ttl_seconds=30) is None
            second = await right.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
            assert second is not None and second.fencing_token == first.fencing_token + 1
            with pytest.raises(ResearchRunLeaseLostError):
                await left.start_stage(first, ResearchRunStatus.PLANNING)
            assert (await left.list_stage_attempts(run.run_id))[0].outcome == ResearchStageOutcome.STARTED
            await right.start_stage(second, ResearchRunStatus.PLANNING)
            results = await asyncio.gather(
                right.finish_stage(second, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED),
                left.finish_stage(second, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED),
                return_exceptions=True,
            )
            assert len([x for x in results if not isinstance(x, Exception)]) == 1
            assert len([x for x in results if isinstance(x, ResearchStageJournalConflictError)]) == 1
            journal = await right.list_stage_attempts(run.run_id)
            assert len(journal) == 2
            assert [x.outcome for x in journal] == [ResearchStageOutcome.STARTED, ResearchStageOutcome.COMPLETED]
            assert (await right.get(run.run_id)).status == ResearchRunStatus.PLANNING
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())


def test_stage_finish_waits_for_same_run_row_lock() -> None:
    async def exercise():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        repo = PostgresResearchRunRepository(sessions)
        task = ResearchAggregate.create("Lock stage journal write")
        run = ResearchRun.create(task.task.task_id)
        try:
            await aggregates.save(task)
            await repo.save(run)
            lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert lease is not None
            run.advance(ResearchRunStatus.PLANNING)
            await repo.save_guarded(run, lease, expected_status=ResearchRunStatus.PENDING)
            await repo.start_stage(lease, ResearchRunStatus.PLANNING)
            async with sessions() as blocker:
                async with blocker.begin():
                    await blocker.execute(
                        select(ResearchRunRow.id).where(ResearchRunRow.id == run.run_id).with_for_update()
                    )
                    finishing = asyncio.create_task(
                        repo.finish_stage(lease, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
                    )
                    await asyncio.sleep(0.05)
                    assert not finishing.done()
                committed = await asyncio.wait_for(finishing, timeout=2)
            assert committed.outcome == ResearchStageOutcome.COMPLETED
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())
