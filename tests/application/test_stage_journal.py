"""Stage journal semantics and failure windows without a live database."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchRunLease,
    ResearchRunLeaseLostError,
    ResearchStageAttempt,
    ResearchStageJournalConflictError,
    ResearchStageOutcome,
)
from enterprise_research.application.errors import ResearchRunNotFoundError
from enterprise_research.application.run_events import ResearchRunEventBus
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure import InMemoryResearchRepository, InMemoryResearchRunRepository


@dataclass
class ManualClock:
    current: datetime = datetime(2026, 10, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


async def setup(*, clock=None, repository_type=InMemoryResearchRunRepository):
    research = InMemoryResearchRepository()
    repository = repository_type(clock=clock) if clock else repository_type()
    app = ResearchApplicationService(research)
    task = await app.create_task("Journal verification")
    runs = ResearchRunApplicationService(research, repository)
    run = await runs.create_run(task.task.task_id)
    return repository, runs, run


async def begin_planning(runs, run, *, owner=None, ttl_seconds=30):
    owner = owner or uuid4()
    lease = await runs.claim_run_lease(run.task_id, run.run_id, owner, ttl_seconds=ttl_seconds)
    assert lease is not None
    await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
    return lease


async def wait_idle(executor):
    async def poll():
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


def test_stage_attempt_model_invariants() -> None:
    now = datetime.now(UTC)
    base = dict(
        run_id=uuid4(),
        stage=ResearchRunStatus.PLANNING,
        fencing_token=1,
        outcome=ResearchStageOutcome.STARTED,
        started_at=now,
    )
    assert ResearchStageAttempt(**base).finished_at is None
    with pytest.raises(ValueError, match="nonterminal"):
        ResearchStageAttempt(**(base | {"stage": ResearchRunStatus.COMPLETED}))
    with pytest.raises(ValueError, match="positive"):
        ResearchStageAttempt(**(base | {"fencing_token": 0}))
    with pytest.raises(ValueError, match="positive"):
        ResearchStageAttempt(**(base | {"fencing_token": True}))
    with pytest.raises(ValueError, match="outcome must"):
        ResearchStageAttempt(**(base | {"outcome": "completed", "finished_at": now}))
    with pytest.raises(ValueError, match="nonterminal"):
        ResearchStageAttempt(**(base | {"stage": "planning"}))
    with pytest.raises(ValueError, match="timezone-aware"):
        ResearchStageAttempt(**(base | {"started_at": now.replace(tzinfo=None)}))
    with pytest.raises(ValueError, match="finish"):
        ResearchStageAttempt(**(base | {"outcome": ResearchStageOutcome.COMPLETED}))
    with pytest.raises(ValueError, match="cannot"):
        ResearchStageAttempt(**(base | {"error": "failure"}))
    with pytest.raises(ValueError, match="precede"):
        ResearchStageAttempt(
            **(base | {"outcome": ResearchStageOutcome.COMPLETED, "finished_at": now - timedelta(seconds=1)})
        )


def test_started_is_not_completed_and_read_only_history_is_stable() -> None:
    async def exercise():
        clock = ManualClock()
        repo, runs, run = await setup(clock=clock)
        lease = await begin_planning(runs, run)
        attempt = await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        assert attempt.outcome == ResearchStageOutcome.STARTED
        assert attempt.finished_at is None
        clock.advance(1)
        assert await runs.inspect_stage_attempts(run.task_id, run.run_id) == [attempt]
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING
        completed = await runs.finish_stage(
            run.task_id,
            run.run_id,
            ResearchRunStatus.PLANNING,
            lease=lease,
            outcome=ResearchStageOutcome.COMPLETED,
        )
        assert completed.finished_at == clock.current
        assert completed.error is None
        assert completed.started_at == attempt.started_at
        assert (await runs.inspect_stage_attempts(run.task_id, run.run_id)) == [completed]
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING

    asyncio.run(exercise())


def test_stage_journal_rejects_missing_duplicate_and_invalid_finish() -> None:
    async def exercise():
        _, runs, run = await setup()
        lease = await begin_planning(runs, run)
        with pytest.raises(ResearchStageJournalConflictError, match="missing"):
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.COMPLETED
            )
        with pytest.raises(ValueError, match="only"):
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.STARTED
            )
        with pytest.raises(ValueError, match="nonblank"):
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=lease,
                outcome=ResearchStageOutcome.FAILED,
                error="  ",
            )
        with pytest.raises(ValueError, match="cannot"):
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=lease,
                outcome=ResearchStageOutcome.COMPLETED,
                error="nope",
            )
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        with pytest.raises(ResearchStageJournalConflictError, match="already started"):
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        finished = await runs.finish_stage(
            run.task_id,
            run.run_id,
            ResearchRunStatus.PLANNING,
            lease=lease,
            outcome=ResearchStageOutcome.FAILED,
            error="  timeout  ",
        )
        assert finished.error == "timeout"
        with pytest.raises(ResearchStageJournalConflictError, match="already finished"):
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.COMPLETED
            )

    asyncio.run(exercise())


def test_stage_journal_rejects_out_of_order_wrong_owner_and_cross_run() -> None:
    async def exercise():
        _, runs, run = await setup()
        lease = await begin_planning(runs, run)
        with pytest.raises(ResearchRunLeaseLostError):
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.SEARCHING, lease=lease)
        with pytest.raises(ValueError, match="nonterminal"):
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.COMPLETED, lease=lease)
        forged = ResearchRunLease(lease.run_id, uuid4(), lease.fencing_token, lease.expires_at)
        with pytest.raises(ResearchRunLeaseLostError):
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=forged)
        forged_run = ResearchRunLease(uuid4(), lease.owner_id, lease.fencing_token, lease.expires_at)
        with pytest.raises(ResearchRunLeaseLostError, match="different run"):
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=forged_run)
        with pytest.raises(ResearchRunLeaseLostError, match="different run"):
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=forged_run,
                outcome=ResearchStageOutcome.COMPLETED,
            )
        other_task = uuid4()
        with pytest.raises(ResearchRunNotFoundError):
            await runs.start_stage(other_task, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        with pytest.raises(ResearchRunNotFoundError):
            await runs.inspect_stage_attempts(other_task, run.run_id)
        assert await runs.inspect_stage_attempts(run.task_id, run.run_id) == []

    asyncio.run(exercise())


def test_expired_and_replaced_generation_cannot_finish_old_attempt() -> None:
    async def exercise():
        clock = ManualClock()
        repo, runs, run = await setup(clock=clock)
        old = await begin_planning(runs, run, ttl_seconds=5)
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
        clock.advance(6)
        with pytest.raises(ResearchRunLeaseLostError):
            await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old, outcome=ResearchStageOutcome.COMPLETED
            )
        assert await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10) is None
        current = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=10, allow_inflight=True)
        assert current is not None and current.fencing_token == 2
        with pytest.raises(ResearchRunLeaseLostError):
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=old,
                outcome=ResearchStageOutcome.FAILED,
                error="stale",
            )
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=current)
        await runs.finish_stage(
            run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=current, outcome=ResearchStageOutcome.COMPLETED
        )
        read = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert [r.fencing_token for r in read] == [1, 2]
        assert [r.outcome for r in read] == [ResearchStageOutcome.STARTED, ResearchStageOutcome.COMPLETED]

    asyncio.run(exercise())


def test_simultaneous_finishes_are_serialized_and_only_one_wins() -> None:
    async def exercise():
        _, runs, run = await setup()
        lease = await begin_planning(runs, run)
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)

        async def finish():
            return await runs.finish_stage(
                run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.COMPLETED
            )

        outcomes = await asyncio.gather(finish(), finish(), return_exceptions=True)
        assert len([v for v in outcomes if isinstance(v, ResearchStageAttempt)]) == 1
        assert len([v for v in outcomes if isinstance(v, ResearchStageJournalConflictError)]) == 1
        assert len(await runs.inspect_stage_attempts(run.task_id, run.run_id)) == 1

    asyncio.run(exercise())


def test_executor_records_five_successful_attempts_and_terminal_run() -> None:
    async def exercise():
        _, runs, run = await setup()

        class Noop:
            async def run_stage(self, *_args):
                await asyncio.sleep(0)

        executor = ResearchRunExecutor(runs, Noop())
        executor.start(run.task_id, run.run_id)
        await wait_idle(executor)
        history = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert [item.stage for item in history] == list(executor._STAGES[:-1])
        assert len({item.fencing_token for item in history}) == 1
        assert all(item.outcome == ResearchStageOutcome.COMPLETED and item.finished_at for item in history)
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.COMPLETED

    asyncio.run(exercise())


def test_executor_records_failure_but_does_not_complete_the_failed_stage() -> None:
    async def exercise():
        _, runs, run = await setup()

        class FailOnSearch:
            async def run_stage(self, _task, _run, stage):
                if stage == ResearchRunStatus.SEARCHING:
                    raise RuntimeError("provider is down")

        executor = ResearchRunExecutor(runs, FailOnSearch())
        executor.start(run.task_id, run.run_id)
        await wait_idle(executor)
        history = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert [e.outcome for e in history] == [ResearchStageOutcome.COMPLETED, ResearchStageOutcome.FAILED]
        assert history[-1].error == "provider is down"
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.FAILED

    asyncio.run(exercise())


def test_cancelled_executor_leaves_in_doubt_started_journal_not_a_failure() -> None:
    async def exercise():
        _, runs, run = await setup()
        started, release = asyncio.Event(), asyncio.Event()

        class Block:
            async def run_stage(self, *_args):
                started.set()
                await release.wait()

        executor = ResearchRunExecutor(runs, Block())
        executor.start(run.task_id, run.run_id)
        await asyncio.wait_for(started.wait(), timeout=1)
        await executor.shutdown()
        entries = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert len(entries) == 1 and entries[0].outcome == ResearchStageOutcome.STARTED
        assert entries[0].finished_at is None
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.PLANNING

    asyncio.run(exercise())


def test_successful_stage_work_with_failed_journal_commit_remains_in_doubt() -> None:
    class CommitFails(InMemoryResearchRunRepository):
        async def finish_stage(self, *_args, **_kwargs):
            raise RuntimeError("journal commit failed")

    async def exercise():
        _, runs, run = await setup(repository_type=CommitFails)
        calls = []

        class Work:
            async def run_stage(self, _task_id, _run_id, stage):
                calls.append(stage)

        executor = ResearchRunExecutor(runs, Work())
        executor.start(run.task_id, run.run_id)
        await wait_idle(executor)
        assert calls == [ResearchRunStatus.PLANNING]
        entries = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert len(entries) == 1 and entries[0].outcome == ResearchStageOutcome.STARTED
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.FAILED

    asyncio.run(exercise())


def test_stage_start_must_commit_before_stage_runner_invoked() -> None:
    class StartFails(InMemoryResearchRunRepository):
        async def start_stage(self, *_args, **_kwargs):
            raise RuntimeError("cannot begin journal")

    async def exercise():
        _, runs, run = await setup(repository_type=StartFails)
        calls = []

        class Work:
            async def run_stage(self, *_args):
                calls.append("called")

        bus = ResearchRunEventBus()
        executor = ResearchRunExecutor(runs, Work(), event_bus=bus)
        executor.start(run.task_id, run.run_id)
        await wait_idle(executor)
        events = [e.event async for e in bus.stream(run.task_id, run.run_id)]
        assert events == ["run.created", "run.failed"]
        assert not calls
        assert await runs.inspect_stage_attempts(run.task_id, run.run_id) == []
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.FAILED

    asyncio.run(exercise())


def test_lease_loss_during_stage_keeps_old_generation_started() -> None:
    async def exercise():
        clock = ManualClock()
        repo, runs, run = await setup(clock=clock)
        entered = asyncio.Event()
        interrupted = asyncio.Event()

        class NeverFinish:
            async def run_stage(self, *_args):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise

        executor = ResearchRunExecutor(runs, NeverFinish(), lease_ttl_seconds=1)
        executor.start(run.task_id, run.run_id)
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock.advance(2)
        next_owner = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=20, allow_inflight=True)
        assert next_owner is not None
        await wait_idle(executor)
        assert interrupted.is_set()
        journal = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        assert len(journal) == 1
        assert journal[0].fencing_token < next_owner.fencing_token
        assert journal[0].outcome == ResearchStageOutcome.STARTED
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.PLANNING

    asyncio.run(exercise())
