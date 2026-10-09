"""Lease fencing and executor ownership regression tests, no database needed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchRunLease,
    ResearchRunLeaseLostError,
)
from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure import InMemoryResearchRepository, InMemoryResearchRunRepository


@dataclass
class ManualClock:
    now: datetime = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


async def make_run(*, clock: ManualClock | None = None):
    repository = InMemoryResearchRunRepository(clock=clock) if clock else InMemoryResearchRunRepository()
    task_repository = InMemoryResearchRepository()
    application = ResearchApplicationService(task_repository)
    task = await application.create_task("Durable ownership test")
    runs = ResearchRunApplicationService(task_repository, repository)
    run = await runs.create_run(task.task.task_id)
    return repository, runs, run


def test_claim_is_exclusive_and_released_reclaim_increments_generation() -> None:
    async def exercise() -> None:
        repo, _, run = await make_run()
        owners = [uuid4() for _ in range(12)]
        claimed = await asyncio.gather(*(repo.claim_lease(run.run_id, o, ttl_seconds=30) for o in owners))
        winners = [lease for lease in claimed if lease is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert winner.fencing_token == 1
        assert winner.owner_id in owners
        assert await repo.claim_lease(run.run_id, winner.owner_id, ttl_seconds=30) is None
        assert await repo.renew_lease(winner, ttl_seconds=30) is not None
        assert await repo.release_lease(winner)
        assert not await repo.release_lease(winner)
        second = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
        assert second is not None and second.fencing_token == 2
        assert not await repo.release_lease(winner)
        assert await repo.renew_lease(winner, ttl_seconds=30) is None
        assert await repo.renew_lease(second, ttl_seconds=30) is not None

    asyncio.run(exercise())


def test_expiry_reclaim_requires_reconciliation_for_inflight_and_rejects_old_writes() -> None:
    async def exercise() -> None:
        clock = ManualClock()
        repo, _, run = await make_run(clock=clock)
        first = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10)
        assert first is not None
        progressed = await repo.get(run.run_id)
        assert progressed is not None
        progressed.advance(ResearchRunStatus.PLANNING)
        await repo.save_guarded(progressed, first, expected_status=ResearchRunStatus.PENDING)
        clock.advance(11)
        assert await repo.renew_lease(first, ttl_seconds=10) is None
        assert not await repo.release_lease(first)
        # The normal executor cannot blind-replay a stage after expiration.
        assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10) is None
        second = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10, allow_inflight=True)
        assert second is not None and second.fencing_token == first.fencing_token + 1
        progressed.advance(ResearchRunStatus.SEARCHING)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save_guarded(progressed, first, expected_status=ResearchRunStatus.PLANNING)
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING
        await repo.save_guarded(progressed, second, expected_status=ResearchRunStatus.PLANNING)
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.SEARCHING

    asyncio.run(exercise())


def test_guarded_transitions_reject_unfenced_stale_duplicate_wrong_run_and_terminal() -> None:
    async def exercise() -> None:
        repo, _, run = await make_run()
        other = ResearchRun.create(run.task_id)
        await repo.save(other)
        lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
        assert lease is not None
        active = await repo.get(run.run_id)
        assert active is not None
        active.advance(ResearchRunStatus.PLANNING)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save(active)
        wrong_run = ResearchRunLease(other.run_id, lease.owner_id, lease.fencing_token, lease.expires_at)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save_guarded(active, wrong_run, expected_status=ResearchRunStatus.PENDING)
        wrong_owner = ResearchRunLease(lease.run_id, uuid4(), lease.fencing_token, lease.expires_at)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save_guarded(active, wrong_owner, expected_status=ResearchRunStatus.PENDING)
        await repo.save_guarded(active, lease, expected_status=ResearchRunStatus.PENDING)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save_guarded(active, lease, expected_status=ResearchRunStatus.PENDING)
        assert await repo.release_lease(lease)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save(active)
        assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30) is None
        assert await repo.claim_lease(other.run_id, uuid4(), ttl_seconds=30) is not None

    asyncio.run(exercise())


def test_ttl_owner_and_expiration_validation() -> None:
    async def exercise() -> None:
        repo, _, run = await make_run()
        for invalid in (0, -1, 3601, True, 1.5, "10"):
            with pytest.raises(ValueError, match="ttl_seconds"):
                await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=invalid)
        assert await repo.claim_lease(uuid4(), uuid4(), ttl_seconds=30) is None
        with pytest.raises(ValueError, match="positive"):
            ResearchRunLease(run.run_id, uuid4(), 0, datetime.now(UTC))
        with pytest.raises(ValueError, match="timezone-aware"):
            ResearchRunLease(run.run_id, uuid4(), 1, datetime.now().replace(tzinfo=None))

    asyncio.run(exercise())


class BlockingStage:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.unblock = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def run_stage(self, _task_id: UUID, _run_id: UUID, stage: ResearchRunStatus) -> None:
        if stage == ResearchRunStatus.PLANNING:
            self.calls += 1
            self.started.set()
            try:
                await self.unblock.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


async def idle(executor: ResearchRunExecutor):
    async def wait():
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=3)


def test_two_independent_executors_run_a_pending_task_at_most_once() -> None:
    async def exercise() -> None:
        _, service, run = await make_run()
        stage = BlockingStage()
        a = ResearchRunExecutor(service, stage)
        b = ResearchRunExecutor(service, stage)
        a.start(run.task_id, run.run_id)
        await asyncio.wait_for(stage.started.wait(), timeout=1)
        b.start(run.task_id, run.run_id)
        await idle(b)
        assert stage.calls == 1
        stage.unblock.set()
        await idle(a)
        assert (await service.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.COMPLETED

    asyncio.run(exercise())


def test_heartbeat_keeps_long_running_stage_owned_until_completion() -> None:
    async def exercise() -> None:
        repo, service, run = await make_run()
        stage = BlockingStage()
        worker = ResearchRunExecutor(service, stage, lease_ttl_seconds=1)
        worker.start(run.task_id, run.run_id)
        await asyncio.wait_for(stage.started.wait(), timeout=1)
        await asyncio.sleep(1.2)
        assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=1, allow_inflight=True) is None
        stage.unblock.set()
        await idle(worker)
        assert (await service.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.COMPLETED

    asyncio.run(exercise())


def test_losing_a_lease_during_a_stage_cancels_without_failing_the_run() -> None:
    async def exercise() -> None:
        clock = ManualClock()
        repo, service, run = await make_run(clock=clock)
        stage = BlockingStage()
        worker = ResearchRunExecutor(service, stage, lease_ttl_seconds=1)
        worker.start(run.task_id, run.run_id)
        await asyncio.wait_for(stage.started.wait(), timeout=1)
        clock.advance(2)
        next_lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=5, allow_inflight=True)
        assert next_lease is not None and next_lease.fencing_token == 2
        await idle(worker)
        assert stage.cancelled
        persisted = await service.get_run(run.task_id, run.run_id)
        assert persisted.status == ResearchRunStatus.PLANNING
        assert persisted.error is None
        await service.advance_run(run.task_id, run.run_id, ResearchRunStatus.SEARCHING, lease=next_lease)
        assert (await service.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.SEARCHING

    asyncio.run(exercise())


def test_pending_lease_can_be_reclaimed_after_expiration_without_clock_skew() -> None:
    async def exercise() -> None:
        clock = ManualClock()
        repo, _, run = await make_run(clock=clock)
        first = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=5)
        assert first is not None
        clock.advance(5)
        assert await repo.renew_lease(first, ttl_seconds=5) is None
        replacement = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=5)
        assert replacement is not None and replacement.fencing_token == 2
        assert not await repo.release_lease(first)
        assert await repo.release_lease(replacement)

    asyncio.run(exercise())


def test_simultaneous_same_generation_transitions_use_status_compare_and_swap() -> None:
    async def exercise() -> None:
        repo, _, run = await make_run()
        lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
        assert lease is not None
        left = await repo.get(run.run_id)
        right = await repo.get(run.run_id)
        assert left is not None and right is not None
        left.advance(ResearchRunStatus.PLANNING)
        right.advance(ResearchRunStatus.PLANNING)
        outcomes = await asyncio.gather(
            repo.save_guarded(left, lease, expected_status=ResearchRunStatus.PENDING),
            repo.save_guarded(right, lease, expected_status=ResearchRunStatus.PENDING),
            return_exceptions=True,
        )
        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, ResearchRunLeaseLostError) for outcome in outcomes) == 1
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING

    asyncio.run(exercise())


def test_terminal_runs_cannot_be_claimed_even_by_explicit_recovery() -> None:
    async def exercise() -> None:
        repo = InMemoryResearchRunRepository()
        completed = ResearchRun.create(uuid4())
        completed.status = ResearchRunStatus.COMPLETED
        failed = ResearchRun.create(uuid4())
        failed.fail("failure already persisted")
        for run in (completed, failed):
            await repo.save(run)
            assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10) is None
            assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10, allow_inflight=True) is None

    asyncio.run(exercise())
