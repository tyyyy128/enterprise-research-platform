"""5B: synchronized two-worker PostgreSQL ownership / commit races.

Opt-in on a migrated local *development* database.  Every test owns random
Task/Run identifiers and deletes only its own Task.  No shared service, PID,
checkpoint table, or database is modified outside those records.

These are transactional concurrency tests, not distributed multi-host tests.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, literal_column, select, update

from enterprise_research.application import (
    ResearchGraphCheckpointReference,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchStageOutcome,
)
from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import (
    Evidence,
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.document_repository import PostgresRetrievedDocumentRepository
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import ResearchRunRow
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_DUAL_WORKER_TESTS") != "1",
    reason="5B races require two explicit flags and a migrated local PostgreSQL development database.",
)


async def _new_database():
    """Reject remote / arbitrary databases before any new test records are saved."""
    settings = PostgresSettings.from_env()
    if settings.host not in {"127.0.0.1", "localhost", "::1"} or settings.database != "enterprise_research":
        raise RuntimeError("5B only runs against the local enterprise_research development database.")
    engine = build_async_engine(settings)
    try:
        async with engine.connect() as conn:
            actual = await conn.scalar(select(func.current_database()))
            if actual != "enterprise_research":
                raise RuntimeError("PostgreSQL did not connect to the expected development database.")
    except BaseException:
        await engine.dispose()
        raise
    return engine, build_session_factory(engine)


async def _cleanup(sessions, task_id: UUID, engine) -> None:
    """Delete only the test's random Task (FK cascades clean its Run/Journal)."""
    try:
        async with sessions() as session:
            async with session.begin():
                await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task_id))
    finally:
        await engine.dispose()


async def _must_remain_blocked(job: asyncio.Task, *, seconds: float = 0.08) -> None:
    """Timeout without cancelling a SQL operation that must wait on a row lock."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(job), timeout=seconds)
    assert not job.done()


async def _expire_lease(sessions, run_id: UUID) -> None:
    """Test-only database expiry manipulation; never touch unrelated Runs."""
    async with sessions() as session:
        async with session.begin():
            assert (
                await session.execute(
                    update(ResearchRunRow)
                    .where(ResearchRunRow.id == run_id)
                    .values(lease_expires_at=func.clock_timestamp() - literal_column("INTERVAL '1 second'"))
                    .returning(ResearchRunRow.id)
                )
            ).scalar_one() == run_id


async def _wait_db_expiry(sessions, run_id: UUID) -> None:
    """Use PostgreSQL wall time (not client wall time) while holding a row lock."""
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        async with sessions() as session:
            expired = await session.scalar(
                select(ResearchRunRow.lease_expires_at <= func.clock_timestamp()).where(ResearchRunRow.id == run_id)
            )
        if expired:
            return
        await asyncio.sleep(0.025)
    raise AssertionError("PostgreSQL lease did not expire within the test timeout")


async def _advance_to(repo, run: ResearchRun, lease: ResearchRunLease, desired: ResearchRunStatus) -> None:
    stages = (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
    )
    for status in stages:
        previous = run.status
        run.advance(status)
        await repo.save_guarded(run, lease, expected_status=previous)
        if status == desired:
            return
    raise AssertionError(f"Unexpected stage: {desired}")


def test_pending_workers_block_on_same_row_and_only_one_claims():
    """Both claims are in flight while a third transaction locks the Run."""

    async def exercise():
        engine, sessions = await _new_database()
        aggregate = ResearchAggregate.create("5B synchronized initial claim")
        run = ResearchRun.create(aggregate.task.task_id)
        attempts: list[asyncio.Task] = []
        try:
            research = PostgresResearchRepository(sessions)
            left, right = PostgresResearchRunRepository(sessions), PostgresResearchRunRepository(sessions)
            await research.save(aggregate)
            await left.save(run)
            launch = asyncio.Event()
            submitted = [asyncio.Event(), asyncio.Event()]
            owners = [uuid4(), uuid4()]

            async def claim(index, repo):
                await launch.wait()
                submitted[index].set()
                return await repo.claim_lease(run.run_id, owners[index], ttl_seconds=30)

            async with sessions() as blocker:
                async with blocker.begin():
                    await blocker.execute(
                        select(ResearchRunRow.id).where(ResearchRunRow.id == run.run_id).with_for_update()
                    )
                    attempts = [asyncio.create_task(claim(0, left)), asyncio.create_task(claim(1, right))]
                    launch.set()
                    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in submitted)), timeout=3)
                    for task in attempts:
                        await _must_remain_blocked(task)
                results = await asyncio.wait_for(asyncio.gather(*attempts), timeout=6)
            winners = [x for x in results if x is not None]
            assert len(winners) == 1
            assert winners[0].owner_id in owners and winners[0].fencing_token == 1
            assert await right.claim_lease(run.run_id, uuid4(), ttl_seconds=30) is None
            assert await left.release_lease(winners[0])
        finally:
            for task in attempts:
                if not task.done():
                    task.cancel()
            if attempts:
                await asyncio.gather(*attempts, return_exceptions=True)
            await _cleanup(sessions, aggregate.task.task_id, engine)

    asyncio.run(exercise())


def test_expired_inflight_takeover_has_one_winner_and_stale_journal_is_fenced():
    """Both contenders wait on a locked, already-expired in-flight Run."""

    async def exercise():
        engine, sessions = await _new_database()
        aggregate = ResearchAggregate.create("5B synchronized inflight takeover")
        run = ResearchRun.create(aggregate.task.task_id)
        attempts: list[asyncio.Task] = []
        try:
            research = PostgresResearchRepository(sessions)
            old_repo, left, right = (PostgresResearchRunRepository(sessions) for _ in range(3))
            await research.save(aggregate)
            await old_repo.save(run)
            old = await old_repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            await _advance_to(old_repo, run, old, ResearchRunStatus.PLANNING)
            await old_repo.start_stage(old, ResearchRunStatus.PLANNING)
            await _expire_lease(sessions, run.run_id)
            assert await old_repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30) is None

            launch = asyncio.Event()
            submitted = [asyncio.Event(), asyncio.Event()]

            async def take(index, repo):
                await launch.wait()
                submitted[index].set()
                return await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)

            async with sessions() as blocker:
                async with blocker.begin():
                    await blocker.execute(
                        select(ResearchRunRow.id).where(ResearchRunRow.id == run.run_id).with_for_update()
                    )
                    attempts = [asyncio.create_task(take(0, left)), asyncio.create_task(take(1, right))]
                    launch.set()
                    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in submitted)), timeout=3)
                    for job in attempts:
                        await _must_remain_blocked(job)
                results = await asyncio.wait_for(asyncio.gather(*attempts), timeout=6)
            winners = [x for x in results if x is not None]
            assert len(winners) == 1 and winners[0].fencing_token == old.fencing_token + 1
            assert not await old_repo.release_lease(old)
            assert await old_repo.renew_lease(old, ttl_seconds=30) is None
            with pytest.raises(ResearchRunLeaseLostError):
                await old_repo.finish_stage(old, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
            with pytest.raises(ResearchRunLeaseLostError):
                await old_repo.start_stage(old, ResearchRunStatus.PLANNING)
            # Raw allow_inflight is a lock probe, NOT reconciliation permission.
            # The old STARTED attempt remains in doubt, and no successor stage
            # should be dispatched or journaled by this test.
            journal = await old_repo.list_stage_attempts(run.run_id)
            assert [(a.fencing_token, a.outcome) for a in journal] == [
                (old.fencing_token, ResearchStageOutcome.STARTED),
            ]
            assert await right.release_lease(winners[0])
        finally:
            for job in attempts:
                if not job.done():
                    job.cancel()
            if attempts:
                await asyncio.gather(*attempts, return_exceptions=True)
            await _cleanup(sessions, aggregate.task.task_id, engine)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["plan", "document", "evidence"])
def test_business_commit_holds_fence_row_until_takeover_then_stale_commit_is_rejected(kind, monkeypatch):
    """A *real repository write transaction* holds the Run lock across expiry.

    The old in-progress transaction may commit after wall-clock expiry if it
    acquired its fence lock before expiry. The successor must wait for it to
    commit before claiming the next generation; it cannot overlap that write.
    """

    async def exercise():
        engine, sessions = await _new_database()
        aggregate = ResearchAggregate.create(f"5B fenced {kind} commit")
        source = Source(source_type=SourceType.WEB, title="Sample", url="https://example.com/5b")
        aggregate.add_source(source)
        run = ResearchRun.create(aggregate.task.task_id)
        entered = asyncio.Event()
        release = asyncio.Event()
        writer: asyncio.Task | None = None
        claimant: asyncio.Task | None = None
        try:
            research = PostgresResearchRepository(sessions)
            runs = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            documents = PostgresRetrievedDocumentRepository(sessions)
            await research.save(aggregate)
            await runs.save(run)
            old = await runs.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            target = {
                "plan": ResearchRunStatus.PLANNING,
                "document": ResearchRunStatus.EXTRACTING,
                "evidence": ResearchRunStatus.VERIFYING,
            }[kind]
            await _advance_to(runs, run, old, target)

            if kind == "plan":
                base = ResearchPlan(
                    task_id=run.task_id,
                    run_id=run.run_id,
                    objective="Initial plan",
                    questions=[ResearchQuestion(question="Which owner committed?")],
                )
                await plans.save_guarded(base, old, run_repository=runs)
                write_value = base.model_copy(update={"objective": "Committed under original row lock"})
                stale_value = base.model_copy(update={"objective": "Forbidden stale overwrite"})
                import enterprise_research.infrastructure.postgres.plan_repository as target_module

                async def commit(lease, value):
                    return await plans.save_guarded(value, lease, run_repository=runs)

                async def read_value():
                    return (await plans.get_for_run(run.run_id)).objective

                committed_expected = "Committed under original row lock"
            elif kind == "document":
                write_value = RetrievedDocument.succeeded(
                    source_id=source.source_id,
                    requested_url="https://example.com/5b",
                    final_url="https://example.com/5b",
                    content="committed document from guarded write",
                )
                stale_value = RetrievedDocument.failed(
                    source_id=source.source_id,
                    requested_url="https://example.com/5b",
                    error="forbidden stale change",
                )
                import enterprise_research.infrastructure.postgres.document_repository as target_module

                async def commit(lease, value):
                    return await documents.save_guarded(
                        value, lease, task_id=run.task_id, run_repository=runs, research_repository=research
                    )

                async def read_value():
                    doc = await documents.get_for_source(source.source_id)
                    return doc.content if doc is not None else None

                committed_expected = "committed document from guarded write"
            else:
                value = await research.get(run.task_id)
                assert value is not None
                value.add_evidence(
                    Evidence(source_id=source.source_id, content="committed evidence", locator="chars:0-9")
                )
                write_value = value
                stale_value = await research.get(run.task_id)
                assert stale_value is not None
                stale_value.add_evidence(
                    Evidence(source_id=source.source_id, content="forbidden evidence", locator="chars:1-8")
                )
                import enterprise_research.infrastructure.postgres.repository as target_module

                async def commit(lease, value):
                    return await research.save_guarded(value, lease, ResearchRunStatus.VERIFYING, run_repository=runs)

                async def read_value():
                    value = await research.get(run.task_id)
                    assert value is not None
                    return sorted(e.content for e in value.evidence.values())

                committed_expected = ["committed evidence"]

            # Patch only the repository's guarded transaction boundary. The
            # real lock_fenced_stage acquires a PostgreSQL FOR UPDATE lock.
            real_guard = lock_fenced_stage

            async def after_fence_lock(session, lease, *, task_id, stage):
                await real_guard(session, lease, task_id=task_id, stage=stage)
                if lease.fencing_token == old.fencing_token:
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=10)

            with monkeypatch.context() as patcher:
                patcher.setattr(target_module, "lock_fenced_stage", after_fence_lock)
                renewed = await runs.renew_lease(old, ttl_seconds=2)
                assert renewed is not None
                writer = asyncio.create_task(commit(renewed, write_value))
                await asyncio.wait_for(entered.wait(), timeout=3)
                # PostgreSQL wall-clock expiry is visible to a plain SELECT
                # even while the writer's row lock is held.
                await _wait_db_expiry(sessions, run.run_id)
                claimant = asyncio.create_task(
                    runs.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
                )
                await _must_remain_blocked(claimant)
                release.set()
                await asyncio.wait_for(writer, timeout=5)
                replacement = await asyncio.wait_for(claimant, timeout=5)
            assert replacement is not None and replacement.fencing_token == old.fencing_token + 1
            assert await read_value() == committed_expected
            with pytest.raises(ResearchRunLeaseLostError):
                await commit(old, stale_value)
            assert await read_value() == committed_expected
            assert not await runs.release_lease(old)
            assert await runs.release_lease(replacement)
        finally:
            release.set()
            for job in (writer, claimant):
                if job is not None and not job.done():
                    job.cancel()
            jobs = [job for job in (writer, claimant) if job is not None]
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await _cleanup(sessions, aggregate.task.task_id, engine)

    asyncio.run(exercise())


def test_journal_finish_holds_run_lock_until_commit_before_takeover(monkeypatch):
    """A real Journal update (not just a separate SELECT) serializes takeover."""

    async def exercise():
        engine, sessions = await _new_database()
        aggregate = ResearchAggregate.create("5B journal finish versus worker takeover")
        run = ResearchRun.create(aggregate.task.task_id)
        entered, release = asyncio.Event(), asyncio.Event()
        finishing: asyncio.Task | None = None
        claiming: asyncio.Task | None = None
        try:
            research = PostgresResearchRepository(sessions)
            old_repo = PostgresResearchRunRepository(sessions)
            new_repo = PostgresResearchRunRepository(sessions)
            await research.save(aggregate)
            await old_repo.save(run)
            old = await old_repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            await _advance_to(old_repo, run, old, ResearchRunStatus.PLANNING)
            await old_repo.start_stage(old, ResearchRunStatus.PLANNING)
            real_require = old_repo._require_stage_lease

            async def after_journal_fence(session, lease, stage):
                await real_require(session, lease, stage)
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=10)

            with monkeypatch.context() as patcher:
                patcher.setattr(old_repo, "_require_stage_lease", after_journal_fence)
                renewed = await old_repo.renew_lease(old, ttl_seconds=2)
                assert renewed is not None
                finishing = asyncio.create_task(
                    old_repo.finish_stage(renewed, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
                )
                await asyncio.wait_for(entered.wait(), timeout=3)
                await _wait_db_expiry(sessions, run.run_id)
                claiming = asyncio.create_task(
                    new_repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
                )
                await _must_remain_blocked(claiming)
                release.set()
                record = await asyncio.wait_for(finishing, timeout=5)
                assert record.outcome == ResearchStageOutcome.COMPLETED
                new = await asyncio.wait_for(claiming, timeout=5)
            assert new is not None and new.fencing_token == old.fencing_token + 1
            with pytest.raises(ResearchRunLeaseLostError):
                await old_repo.finish_stage(old, ResearchRunStatus.PLANNING, outcome=ResearchStageOutcome.COMPLETED)
            journal = await new_repo.list_stage_attempts(run.run_id)
            assert len(journal) == 1 and journal[0].outcome == ResearchStageOutcome.COMPLETED
            assert await new_repo.release_lease(new)
        finally:
            release.set()
            for job in (finishing, claiming):
                if job is not None and not job.done():
                    job.cancel()
            jobs = [job for job in (finishing, claiming) if job is not None]
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await _cleanup(sessions, aggregate.task.task_id, engine)

    asyncio.run(exercise())


class _PreclaimGate:
    """Force both independent executors to finish their first inspection."""

    def __init__(self, inspector):
        self.inspector = inspector
        self.arrived = 0
        self.release = asyncio.Event()

    async def inspect(self, task_id, run_id):
        decision = await self.inspector.inspect(task_id, run_id)
        self.arrived += 1
        if self.arrived == 2:
            self.release.set()
        if self.arrived <= 2:
            await asyncio.wait_for(self.release.wait(), timeout=10)
        return decision


class _BlockingResearchStage:
    def __init__(self):
        self.calls: list[ResearchRunStatus] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run_stage(self, _task_id, _run_id, stage, *, lease=None):
        assert lease is not None
        self.calls.append(stage)
        if stage == ResearchRunStatus.SEARCHING:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=5)


class _StableReader:
    async def latest(self, binding):
        return ResearchGraphCheckpointReference(binding, "checkpoint-A")


def test_two_recovery_executors_both_pass_preflight_but_only_one_dispatches():
    """Use real PostgreSQL claims after a barrier at the pre-claim proof."""

    async def exercise():
        engine, sessions = await _new_database()
        aggregate = ResearchAggregate.create("5B concurrent explicit safe resume")
        run = ResearchRun.create(aggregate.task.task_id)
        executors: list[ResearchRunExecutor] = []
        runners: list[_BlockingResearchStage] = []
        try:
            research = PostgresResearchRepository(sessions)
            repository = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            docs = PostgresRetrievedDocumentRepository(sessions)
            runs = ResearchRunApplicationService(research, repository)
            await research.save(aggregate)
            await repository.save(run)
            old = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
            await plans.save_guarded(
                ResearchPlan(
                    task_id=run.task_id,
                    run_id=run.run_id,
                    objective="Durable planning artifact",
                    questions=[ResearchQuestion(question="How many worker owners?")],
                ),
                old,
                run_repository=repository,
            )
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=old,
                outcome=ResearchStageOutcome.COMPLETED,
            )
            assert await runs.release_run_lease(old)
            inspector = ResearchSafeResumeInspector(runs, research, plans, docs, graph_checkpoints=_StableReader())
            first = await inspector.inspect(run.task_id, run.run_id)
            assert first.disposition == ResearchResumeDisposition.READY
            gate = _PreclaimGate(inspector)
            runners = [_BlockingResearchStage(), _BlockingResearchStage()]
            executors = [ResearchRunExecutor(runs, runner, resume_inspector=gate) for runner in runners]
            assert executors[0]._owner_id != executors[1]._owner_id
            results = await asyncio.wait_for(
                asyncio.gather(*(executor.resume(run.task_id, run.run_id) for executor in executors)), timeout=15
            )
            assert gate.arrived >= 2 and sorted(results) == [False, True]
            winning_runner = runners[results.index(True)]
            await asyncio.wait_for(winning_runner.entered.wait(), timeout=3)
            assert [r.calls.count(ResearchRunStatus.SEARCHING) for r in runners] in ([1, 0], [0, 1])
            assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.SEARCHING
            winning_runner.release.set()

            async def wait_idle():
                deadline = asyncio.get_running_loop().time() + 5
                while any(executor.active_count for executor in executors):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("Winner did not finish its test-only stages")
                    await asyncio.sleep(0.02)

            await wait_idle()
            assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.FAILED
            assert "WRITING proof missing" in ((await runs.get_run(run.task_id, run.run_id)).error or "")
            # The winning executor safely dispatched; its no-op report stage
            # must not create a false terminal success in 7C.
            attempts = await runs.inspect_stage_attempts(run.task_id, run.run_id)
            assert len([a for a in attempts if a.stage == ResearchRunStatus.SEARCHING]) == 1
            assert len([a for a in attempts if a.stage == ResearchRunStatus.PLANNING]) == 1
        finally:
            for runner in runners:
                runner.release.set()
            for executor in executors:
                await executor.shutdown()
            await _cleanup(sessions, aggregate.task.task_id, engine)

    asyncio.run(exercise())
