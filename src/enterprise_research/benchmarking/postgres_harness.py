"""Bounded opt-in PostgreSQL benchmark of real Safe Resume + fenced Plan writes.

The workload deliberately injects expiry for its own UUID Run. It never
kills a service, makes network/LLM calls, or modifies an unrelated Task.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application import ResearchRunApplicationService, ResearchRunExecutor, ResearchStageOutcome
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.application.safe_resume import ResearchSafeResumeInspector
from enterprise_research.benchmarking.metrics import PAUSED, RecoveryBenchmarkSample
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresRetrievedDocumentRepository,
    PostgresSettings,
    ResearchRunRow,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

SCENARIOS = ("ready_two_workers", "started_with_plan", "completed_without_plan", "contended_initial_claim")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    trials_per_scenario: int = 5
    concurrency: int = 2
    warmup_per_scenario: int = 1
    lock_hold_ms: int = 25
    seed: int = 42

    def __post_init__(self) -> None:
        ranges = (
            (self.trials_per_scenario, 1, 100, "trials_per_scenario"),
            (self.concurrency, 1, 4, "concurrency"),
            (self.warmup_per_scenario, 0, 10, "warmup_per_scenario"),
            (self.lock_hold_ms, 10, 150, "lock_hold_ms"),
            (self.seed, 0, 2**32 - 1, "seed"),
        )
        for value, low, high, name in ranges:
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}].")


class _BlockingRunner:
    """Observe exactly one Search dispatch but never perform downstream work."""

    def __init__(self) -> None:
        self.calls: list[ResearchRunStatus] = []
        self.entered = asyncio.Event()

    async def run_stage(self, _task_id: UUID, _run_id: UUID, stage: ResearchRunStatus, *, lease=None) -> None:
        assert lease is not None
        self.calls.append(stage)
        self.entered.set()
        await asyncio.Event().wait()


def _millis(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000


async def _expire_only_test_run(sessions: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
    """Simulate abandoned lease without sleeping TTL seconds or killing a process."""
    async with sessions() as session:
        async with session.begin():
            actual = await session.scalar(
                update(ResearchRunRow)
                .where(ResearchRunRow.id == run_id)
                .values(lease_expires_at=func.clock_timestamp() - literal_column("INTERVAL '1 second'"))
                .returning(ResearchRunRow.id)
            )
            if actual != run_id:
                raise AssertionError("Scoped test lease expiration did not update its own run.")


async def _inspect_and_resume(
    sessions: async_sessionmaker[AsyncSession],
    aggregate: ResearchAggregate,
    run: ResearchRun,
    scenario: str,
    metrics: dict[str, object],
) -> None:
    research = PostgresResearchRepository(sessions)
    repo = PostgresResearchRunRepository(sessions)
    app = ResearchRunApplicationService(research, repo)
    plans = PostgresResearchPlanRepository(sessions)
    old = await app.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
    if old is None:
        raise AssertionError("Isolated pending Run could not be claimed.")
    await app.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
    await app.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)

    plan: ResearchPlan | None = None
    if scenario != "completed_without_plan":
        plan = ResearchPlan(
            task_id=run.task_id,
            run_id=run.run_id,
            objective="5C isolated plan handoff",
            questions=[ResearchQuestion(question="Can the next stage be safely dispatched?")],
        )
        await plans.save_guarded(plan, old, run_repository=repo)
    if scenario != "started_with_plan":
        await app.finish_stage(
            run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old, outcome=ResearchStageOutcome.COMPLETED
        )
    await _expire_only_test_run(sessions, run.run_id)

    # A fresh service/repository pair simulates a new executor process's DB sessions.
    new_repo = PostgresResearchRunRepository(sessions)
    fresh = ResearchRunApplicationService(PostgresResearchRepository(sessions), new_repo)
    inspector = ResearchSafeResumeInspector(
        fresh,
        PostgresResearchRepository(sessions),
        PostgresResearchPlanRepository(sessions),
        PostgresRetrievedDocumentRepository(sessions),
    )
    started = time.perf_counter_ns()
    decision = await inspector.inspect(aggregate.task.task_id, run.run_id)
    metrics["inspect_ms"] = _millis(started)
    metrics["observed_disposition"] = decision.disposition.value

    if scenario == "ready_two_workers":
        left_runner, right_runner = _BlockingRunner(), _BlockingRunner()
        left = ResearchRunExecutor(fresh, left_runner, resume_inspector=inspector)
        right = ResearchRunExecutor(fresh, right_runner, resume_inspector=inspector)
        try:
            started = time.perf_counter_ns()
            outcomes = await asyncio.wait_for(
                asyncio.gather(left.resume(run.task_id, run.run_id), right.resume(run.task_id, run.run_id)),
                timeout=8,
            )
            if any(outcomes):
                winner = left_runner if outcomes[0] else right_runner
                await asyncio.wait_for(winner.entered.wait(), timeout=5)
            metrics["decision_ms"] = _millis(started)
            metrics["dispatch_count"] = len(left_runner.calls) + len(right_runner.calls)
            if sorted(outcomes) != [False, True] or metrics["dispatch_count"] != 1:
                raise AssertionError("Exactly one recovery executor must dispatch the next stage.")
            assert plan is not None
            metrics["stale_write_attempts"] = 1
            try:
                await plans.save_guarded(plan, old, run_repository=repo)
            except ResearchRunLeaseLostError:
                pass
            else:
                metrics["stale_write_accepted"] = 1
            persisted = await plans.get_for_run(run.run_id)
            if persisted != plan:
                raise AssertionError("Persisted plan changed during stale fenced-write probe.")
        finally:
            await asyncio.gather(left.shutdown(), right.shutdown())
    else:
        runner = _BlockingRunner()
        executor = ResearchRunExecutor(fresh, runner, resume_inspector=inspector)
        try:
            started = time.perf_counter_ns()
            outcome = await asyncio.wait_for(executor.resume(run.task_id, run.run_id), timeout=8)
            if outcome:
                await asyncio.wait_for(runner.entered.wait(), timeout=5)
            metrics["decision_ms"] = _millis(started)
            metrics["dispatch_count"] = len(runner.calls)
            if outcome or runner.calls or decision.disposition.value != PAUSED:
                raise AssertionError("Incomplete stage proof must not be resumed.")
        finally:
            await executor.shutdown()


async def _contended_claim(
    sessions: async_sessionmaker[AsyncSession], run: ResearchRun, lock_hold_ms: int, metrics: dict[str, object]
) -> None:
    repo = PostgresResearchRunRepository(sessions)
    entered = asyncio.Event()
    job: asyncio.Task[tuple[object, float]] | None = None

    async def contender() -> tuple[object, float]:
        entered.set()
        start_ns = time.perf_counter_ns()
        lease = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30)
        return lease, _millis(start_ns)

    try:
        async with sessions() as session:
            async with session.begin():
                locked = await session.scalar(
                    select(ResearchRunRow.id).where(ResearchRunRow.id == run.run_id).with_for_update()
                )
                if locked != run.run_id:
                    raise AssertionError("Failed to lock isolated benchmark Run.")
                job = asyncio.create_task(contender())
                await asyncio.wait_for(entered.wait(), timeout=3)
                await asyncio.sleep(lock_hold_ms / 1000)
                if job.done():
                    raise AssertionError("Contender completed before the row lock was released.")
        assert job is not None
        claimed, duration_ms = await asyncio.wait_for(job, timeout=8)
        if claimed is None:
            raise AssertionError("The pending Run claim failed after row lock release.")
        metrics["lock_claim_ms"] = duration_ms
    finally:
        if job is not None and not job.done():
            job.cancel()
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)


async def _one_case(
    sessions: async_sessionmaker[AsyncSession], scenario: str, trial: int, lock_hold_ms: int
) -> RecoveryBenchmarkSample:
    if scenario not in SCENARIOS:
        raise ValueError(f"Unsupported benchmark scenario: {scenario}")
    aggregate = ResearchAggregate.create(f"5C benchmark {scenario} (ephemeral)")
    run = ResearchRun.create(aggregate.task.task_id)
    expected = True if scenario == "ready_two_workers" else None if scenario == "contended_initial_claim" else False
    metrics: dict[str, object] = {
        "observed_disposition": None,
        "dispatch_count": 0,
        "stale_write_attempts": 0,
        "stale_write_accepted": 0,
        "inspect_ms": None,
        "decision_ms": None,
        "lock_claim_ms": None,
    }
    started = time.perf_counter_ns()
    error: str | None = None
    created = False
    try:
        await PostgresResearchRepository(sessions).save(aggregate)
        created = True
        await PostgresResearchRunRepository(sessions).save(run)
        if scenario == "contended_initial_claim":
            await _contended_claim(sessions, run, lock_hold_ms, metrics)
        else:
            await _inspect_and_resume(sessions, aggregate, run, scenario, metrics)
    except Exception as exc:
        # Only exception type appears in reports; database error strings may
        # contain DSNs, server details, or user-provided content.
        error = type(exc).__name__
    finally:
        if created:
            try:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            except Exception:
                error = "CleanupFailed"  # do not report success if test rows survive
    return RecoveryBenchmarkSample(
        scenario=scenario,
        trial=trial,
        expected_ready=expected,
        trial_ms=_millis(started),
        task_id=str(aggregate.task.task_id),
        error=error,
        **metrics,
    )


async def run_postgres_benchmark(config: BenchmarkConfig) -> tuple[list[RecoveryBenchmarkSample], float]:
    """Run bounded real DB experiments; no baseline comparison is implied."""
    if (
        os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
        or os.getenv("ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK") != "1"
    ):
        raise PermissionError("5C requires both explicit PostgreSQL benchmark opt-in environment flags.")
    settings = PostgresSettings.from_env()
    if settings.host not in LOCAL_HOSTS or settings.database != "enterprise_research" or settings.port != 5432:
        raise PermissionError("5C requires the local enterprise_research development database on port 5432.")
    engine = build_async_engine(settings)
    try:
        async with engine.connect() as connection:
            actual = await connection.scalar(select(func.current_database()))
            if actual != "enterprise_research":
                raise PermissionError("Connected PostgreSQL database name does not match the safe development target.")
        sessions = build_session_factory(engine)
        warmups = [(scenario, trial) for trial in range(config.warmup_per_scenario) for scenario in SCENARIOS]
        for scenario, trial in warmups:
            result = await _one_case(sessions, scenario, trial, config.lock_hold_ms)
            if result.error is not None or result.unauthorized_dispatches or result.stale_write_accepted:
                raise RuntimeError(f"Warmup rejected ({scenario}): {result.error or 'safety invariant'}")
            if scenario == "ready_two_workers" and result.dispatch_count != 1:
                raise RuntimeError("Ready warmup did not dispatch one executor.")
            if scenario in {"started_with_plan", "completed_without_plan"} and result.observed_disposition != PAUSED:
                raise RuntimeError("Paused warmup did not produce a conservative pause.")
            if scenario == "contended_initial_claim" and result.lock_claim_ms is None:
                raise RuntimeError("Contended-claim warmup did not measure latency.")

        work = [(scenario, trial) for trial in range(config.trials_per_scenario) for scenario in SCENARIOS]
        random.Random(config.seed).shuffle(work)
        sem = asyncio.Semaphore(config.concurrency)

        async def limited(case: tuple[str, int]) -> RecoveryBenchmarkSample:
            async with sem:
                return await _one_case(sessions, case[0], case[1], config.lock_hold_ms)

        started = time.perf_counter()
        samples = list(await asyncio.gather(*(limited(case) for case in work)))
        duration = time.perf_counter() - started
        return samples, duration
    finally:
        await engine.dispose()
