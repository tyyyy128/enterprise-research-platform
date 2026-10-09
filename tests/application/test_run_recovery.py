"""3E-6D-1 read-only recovery inventory and decision protocol."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
)
from enterprise_research.application.executor import ResearchRunExecutor
from enterprise_research.application.run_recovery import (
    ResearchRunRecoveryCandidate,
    ResearchRunRecoveryDisposition,
)
from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)


@pytest.mark.parametrize(
    "status",
    [
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
    ],
)
def test_in_flight_stages_require_reconciliation(status: ResearchRunStatus) -> None:
    run = ResearchRun.create(uuid4())
    # This model records entering a stage, not completing its side effects.
    run.status = status

    candidate = ResearchRunRecoveryCandidate.from_run(run)

    assert candidate.status == status
    assert candidate.stage_to_reconcile == status
    assert candidate.disposition == ResearchRunRecoveryDisposition.STAGE_RECONCILIATION_REQUIRED
    assert candidate.updated_at == run.updated_at
    assert run.status == status


def test_pending_run_requires_ownership_claim_not_auto_replay() -> None:
    run = ResearchRun.create(uuid4())

    candidate = ResearchRunRecoveryCandidate.from_run(run)

    assert candidate.disposition == ResearchRunRecoveryDisposition.OWNERSHIP_CLAIM_REQUIRED
    assert candidate.stage_to_reconcile is None
    assert run.status == ResearchRunStatus.PENDING


@pytest.mark.parametrize("status", [ResearchRunStatus.COMPLETED, ResearchRunStatus.FAILED])
def test_terminal_runs_have_no_recovery_candidate(status: ResearchRunStatus) -> None:
    run = ResearchRun.create(uuid4())
    run.status = status

    with pytest.raises(ValueError, match="Terminal research runs"):
        ResearchRunRecoveryCandidate.from_run(run)


def test_memory_recovery_inventory_is_bounded_ordered_and_detached() -> None:
    async def run_test() -> None:
        repository = InMemoryResearchRunRepository()
        base = ResearchRun.create(uuid4()).updated_at
        newer = ResearchRun.create(uuid4())
        older = ResearchRun.create(uuid4())
        completed = ResearchRun.create(uuid4())
        failed = ResearchRun.create(uuid4())

        newer.updated_at = base + timedelta(minutes=2)
        older.task_id = newer.task_id
        older.updated_at = base - timedelta(minutes=2)
        completed.status = ResearchRunStatus.COMPLETED
        failed.fail("not recoverable")

        for run in (newer, completed, failed, older):
            await repository.save(run)

        assert [r.run_id for r in await repository.list_unfinished(limit=1)] == [older.run_id]
        listing = await repository.list_unfinished(limit=10)
        assert [r.run_id for r in listing] == [older.run_id, newer.run_id]
        listing[0].status = ResearchRunStatus.FAILED
        assert (await repository.get(older.run_id)).status == ResearchRunStatus.PENDING

        assert await repository.list_unfinished(limit=10, task_id=uuid4()) == []
        assert len(await repository.list_unfinished(limit=10, task_id=newer.task_id)) == 2

        with pytest.raises(ValueError, match="positive"):
            await repository.list_unfinished(limit=0)

    asyncio.run(run_test())


def test_same_update_time_orders_by_run_id() -> None:
    async def run_test() -> None:
        repository = InMemoryResearchRunRepository()
        runs = [ResearchRun.create(uuid4()) for _ in range(3)]
        for item in runs:
            item.updated_at = runs[0].updated_at
        for item in reversed(runs):
            await repository.save(item)
        listing = await repository.list_unfinished(limit=10)
        assert [run.run_id for run in listing] == sorted(run.run_id for run in runs)

    asyncio.run(run_test())


def test_application_inventory_does_not_mutate_persisted_runs() -> None:
    async def run_test() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        application = ResearchApplicationService(research_repository)
        runs = ResearchRunApplicationService(research_repository, run_repository)

        task = await application.create_task("Audit interrupted research")
        pending = await runs.create_run(task.task.task_id)
        active = await runs.create_run(task.task.task_id)
        await runs.advance_run(task.task.task_id, active.run_id, ResearchRunStatus.PLANNING)

        before = {rid: await run_repository.get(rid) for rid in (pending.run_id, active.run_id)}
        candidates = await runs.inspect_unfinished_runs()
        assert {item.run_id for item in candidates} == {pending.run_id, active.run_id}
        assert {item.run_id: item.disposition for item in candidates} == {
            pending.run_id: ResearchRunRecoveryDisposition.OWNERSHIP_CLAIM_REQUIRED,
            active.run_id: ResearchRunRecoveryDisposition.STAGE_RECONCILIATION_REQUIRED,
        }
        assert {rid: await run_repository.get(rid) for rid in before} == before

        for bad_limit in (0, -1, 501):
            with pytest.raises(ValueError, match="between 1 and 500"):
                await runs.inspect_unfinished_runs(limit=bad_limit)
        assert len(await runs.inspect_unfinished_runs(limit=1)) == 1
        assert len(await runs.inspect_unfinished_runs(task_id=task.task.task_id)) == 2
        assert await runs.inspect_unfinished_runs(task_id=uuid4()) == []

    asyncio.run(run_test())


def test_recovery_inventory_does_not_include_failed_or_completed_runs() -> None:
    async def run_test() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        application = ResearchApplicationService(research_repository)
        runs = ResearchRunApplicationService(research_repository, run_repository)
        task = await application.create_task("Recover only unfinished")
        failed = await runs.create_run(task.task.task_id)
        finished = await runs.create_run(task.task.task_id)
        await runs.fail_run(task.task.task_id, failed.run_id, "provider error")
        for stage in (
            ResearchRunStatus.PLANNING,
            ResearchRunStatus.SEARCHING,
            ResearchRunStatus.EXTRACTING,
            ResearchRunStatus.VERIFYING,
            ResearchRunStatus.WRITING,
            ResearchRunStatus.COMPLETED,
        ):
            await runs.advance_run(task.task.task_id, finished.run_id, stage)
        assert await runs.inspect_unfinished_runs() == []

    asyncio.run(run_test())


def test_a_live_stage_is_listed_without_being_cancelled_or_replayed() -> None:
    class BlockingRunner:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[ResearchRunStatus] = []

        async def run_stage(self, task_id: UUID, run_id: UUID, stage: ResearchRunStatus, *, lease=None) -> None:
            del task_id, run_id
            self.calls.append(stage)
            if stage == ResearchRunStatus.PLANNING:
                self.entered.set()
                await self.release.wait()

    async def run_test() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        application = ResearchApplicationService(research_repository)
        runs = ResearchRunApplicationService(research_repository, run_repository)
        task = await application.create_task("A legitimate long-running stage")
        current = await runs.create_run(task.task.task_id)
        stage_runner = BlockingRunner()
        executor = ResearchRunExecutor(runs, stage_runner)
        executor.start(task.task.task_id, current.run_id)
        await asyncio.wait_for(stage_runner.entered.wait(), timeout=1)

        try:
            candidates = await runs.inspect_unfinished_runs()
            assert len(candidates) == 1
            assert candidates[0].stage_to_reconcile == ResearchRunStatus.PLANNING
            assert candidates[0].disposition == ResearchRunRecoveryDisposition.STAGE_RECONCILIATION_REQUIRED
            assert stage_runner.calls == [ResearchRunStatus.PLANNING]
            assert executor.active_count == 1
        finally:
            stage_runner.release.set()

            async def wait_for_completion() -> None:
                while executor.active_count:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_completion(), timeout=1)
            await executor.shutdown()

        assert (await runs.get_run(task.task.task_id, current.run_id)).status == ResearchRunStatus.COMPLETED
        assert await runs.inspect_unfinished_runs() == []

    asyncio.run(run_test())
