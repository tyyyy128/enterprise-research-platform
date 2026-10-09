"""Real PostgreSQL lease concurrency checks; opt-in on a migrated dev database."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, literal_column, update

from enterprise_research.application.run_lease import ResearchRunLeaseLostError
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


def test_postgres_two_workers_contend_then_stale_worker_cannot_write() -> None:
    async def exercise() -> None:
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        left = PostgresResearchRunRepository(sessions)
        right = PostgresResearchRunRepository(sessions)
        task = ResearchAggregate.create("Two worker atomic claim")
        run = ResearchRun.create(task.task.task_id)
        try:
            await aggregates.save(task)
            await left.save(run)
            owners = (uuid4(), uuid4())
            first, second = await asyncio.gather(
                left.claim_lease(run.run_id, owners[0], ttl_seconds=30),
                right.claim_lease(run.run_id, owners[1], ttl_seconds=30),
            )
            leases = [lease for lease in (first, second) if lease is not None]
            assert len(leases) == 1
            lease = leases[0]
            assert lease.fencing_token == 1
            assert await right.claim_lease(run.run_id, lease.owner_id, ttl_seconds=30) is None
            with pytest.raises(ResearchRunLeaseLostError):
                await left.save(run)
            live = await right.renew_lease(lease, ttl_seconds=30)
            assert live is not None and live.fencing_token == lease.fencing_token
            assert await left.release_lease(lease)
            replacement = await right.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert replacement is not None and replacement.fencing_token == 2
            assert not await left.release_lease(lease)
            assert await left.renew_lease(lease, ttl_seconds=30) is None
            run.advance(ResearchRunStatus.PLANNING)
            with pytest.raises(ResearchRunLeaseLostError):
                await left.save_guarded(run, lease, expected_status=ResearchRunStatus.PENDING)
            assert (await right.get(run.run_id)).status == ResearchRunStatus.PENDING
            await right.save_guarded(run, replacement, expected_status=ResearchRunStatus.PENDING)
            assert (await left.get(run.run_id)).status == ResearchRunStatus.PLANNING
            with pytest.raises(ResearchRunLeaseLostError):
                await right.save_guarded(run, replacement, expected_status=ResearchRunStatus.PENDING)
            assert await right.release_lease(replacement)
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_expired_inflight_lease_needs_explicit_reconciled_takeover() -> None:
    async def exercise() -> None:
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        repo = PostgresResearchRunRepository(sessions)
        new_process = PostgresResearchRunRepository(sessions)
        task = ResearchAggregate.create("Reconcile orphaned run")
        run = ResearchRun.create(task.task.task_id)
        try:
            await aggregates.save(task)
            await repo.save(run)
            lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert lease is not None
            run.advance(ResearchRunStatus.PLANNING)
            await repo.save_guarded(run, lease, expected_status=ResearchRunStatus.PENDING)
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchRunRow)
                        .where(ResearchRunRow.id == run.run_id)
                        .values(lease_expires_at=func.clock_timestamp() - literal_column("INTERVAL '1 second'"))
                    )
            assert await repo.renew_lease(lease, ttl_seconds=30) is None
            assert await new_process.claim_lease(run.run_id, uuid4(), ttl_seconds=30) is None
            takeover = await new_process.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
            assert takeover is not None and takeover.fencing_token == 2
            run.advance(ResearchRunStatus.SEARCHING)
            with pytest.raises(ResearchRunLeaseLostError):
                await repo.save_guarded(run, lease, expected_status=ResearchRunStatus.PLANNING)
            await new_process.save_guarded(run, takeover, expected_status=ResearchRunStatus.PLANNING)
            assert (await repo.get(run.run_id)).status == ResearchRunStatus.SEARCHING
            assert await new_process.release_lease(takeover)
            with pytest.raises(ResearchRunLeaseLostError):
                await repo.save(run)
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task.task_id))
            await engine.dispose()

    asyncio.run(exercise())
