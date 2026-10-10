"""Opt-in 5C-2 paired strategy experiment using PostgreSQL TEMP tables only.

No ResearchTask, Run, stage journal or production side-effect table is read
or written. A fresh temporary SQL ledger models a single PLANNING-stage effect.
The guarded policy is an *illustrative model*, not the real Safe Resume API.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from enterprise_research.benchmarking.postgres_harness import LOCAL_HOSTS
from enterprise_research.benchmarking.strategy_comparison import (
    FAULT_WINDOWS,
    Action,
    Checkpoint,
    ComparisonSample,
    FaultWindow,
    Observation,
    Strategy,
    decide,
    summarize_comparison,
)
from enterprise_research.infrastructure.postgres import PostgresSettings, build_async_engine

# Names are session-local. All SQL below explicitly targets pg_temp after creation.
_CREATE_STAGE = """
CREATE TEMP TABLE er_5c2_stage (
    journal_completed BOOLEAN NOT NULL,
    journal_attempts INTEGER NOT NULL,
    checkpoint_state TEXT NOT NULL,
    generation INTEGER NOT NULL
) ON COMMIT PRESERVE ROWS
"""
_CREATE_EFFECTS = """
CREATE TEMP TABLE er_5c2_effects (
    effect_kind TEXT NOT NULL,
    writer_generation INTEGER NOT NULL
) ON COMMIT PRESERVE ROWS
"""


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    trials_per_window: int = 5
    warmup_per_window: int = 1
    concurrency: int = 2
    seed: int = 42

    def __post_init__(self) -> None:
        for name, minimum, maximum in (
            ("trials_per_window", 1, 100),
            ("warmup_per_window", 0, 5),
            ("concurrency", 1, 4),
            ("seed", 0, 2**32 - 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}].")


def _ms(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000


async def _create_temporary_ledger(connection: AsyncConnection) -> None:
    # CREATE TEMP cannot modify existing production tables; a pooled connection
    # must release these session-local tables before it can be reused.
    async with connection.begin():
        await connection.execute(text(_CREATE_STAGE))
        await connection.execute(text(_CREATE_EFFECTS))


async def _reset_snapshot(connection: AsyncConnection, window: FaultWindow) -> None:
    async with connection.begin():
        await connection.execute(text("TRUNCATE pg_temp.er_5c2_effects, pg_temp.er_5c2_stage"))
        await connection.execute(
            text(
                "INSERT INTO pg_temp.er_5c2_stage "
                "(journal_completed, journal_attempts, checkpoint_state, generation) "
                "VALUES (:journal, :attempts, :checkpoint, 2)"
            ),
            {
                "journal": window.journal_completed,
                "attempts": window.journal_attempts,
                "checkpoint": window.checkpoint.value,
            },
        )
        if window.artifact_committed:
            await connection.execute(
                text("INSERT INTO pg_temp.er_5c2_effects (effect_kind, writer_generation) VALUES ('stage', 1)")
            )


async def _observe(connection: AsyncConnection, strategy: Strategy) -> Observation:
    if strategy == Strategy.NAIVE_RETRY:
        return Observation()
    if strategy == Strategy.CHECKPOINT_ONLY:
        value = await connection.scalar(text("SELECT checkpoint_state FROM pg_temp.er_5c2_stage"))
        return Observation(checkpoint=Checkpoint(value))
    row = (
        await connection.execute(
            text(
                "SELECT journal_completed, checkpoint_state, journal_attempts, "
                "(SELECT count(*) FROM pg_temp.er_5c2_effects WHERE effect_kind = 'stage') AS artifact_count "
                "FROM pg_temp.er_5c2_stage"
            )
        )
    ).one()
    return Observation(
        checkpoint=Checkpoint(row[1]),
        journal_completed=row[0],
        journal_attempts=row[2],
        artifact_count=row[3],
    )


async def _one_policy(
    connection: AsyncConnection, strategy: Strategy, window: FaultWindow, trial: int
) -> ComparisonSample:
    started = time.perf_counter_ns()
    action: Action | None = None
    decision_ms: float | None = None
    operation_ms: float | None = None
    count_after: int | None = None
    stale_accepted = 0
    error: str | None = None
    try:
        await _reset_snapshot(connection, window)
        operation_started = time.perf_counter_ns()
        async with connection.begin():
            decision_started = time.perf_counter_ns()
            observation = await _observe(connection, strategy)
            action = decide(strategy, observation)
            decision_ms = _ms(decision_started)
            if action == Action.RETRY_STAGE:
                await connection.execute(
                    text("INSERT INTO pg_temp.er_5c2_effects (effect_kind, writer_generation) VALUES ('stage', 2)")
                )
            if window.old_worker_attempt:
                if strategy == Strategy.GUARDED:
                    await connection.execute(
                        text(
                            "INSERT INTO pg_temp.er_5c2_effects (effect_kind, writer_generation) "
                            "SELECT 'stale', 1 FROM pg_temp.er_5c2_stage WHERE generation = 1"
                        )
                    )
                else:
                    await connection.execute(
                        text("INSERT INTO pg_temp.er_5c2_effects (effect_kind, writer_generation) VALUES ('stale', 1)")
                    )
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "count(*) FILTER (WHERE effect_kind = 'stage'), "
                        "count(*) FILTER (WHERE effect_kind = 'stale') "
                        "FROM pg_temp.er_5c2_effects"
                    )
                )
            ).one()
            count_after = counts[0]
            stale_accepted = counts[1]
        operation_ms = _ms(operation_started)
    except Exception as exc:
        # PostgreSQL errors can include DSNs; never serialize their messages.
        error = type(exc).__name__
    duplicated_effects = max(0, (count_after or 0) - 1)
    missing_skip = int(action == Action.DISPATCH_NEXT and count_after == 0)
    unproven = int(action == Action.DISPATCH_NEXT and not window.proven_for_next)
    return ComparisonSample(
        strategy=strategy.value,
        window=window.name,
        trial=trial,
        action=action.value if action is not None else None,
        artifacts_before=int(window.artifact_committed),
        artifacts_after=count_after,
        duplicated_effects=duplicated_effects,
        missing_artifact_skips=missing_skip,
        unproven_next_dispatches=unproven,
        stale_write_attempts=int(window.old_worker_attempt),
        stale_write_accepted=stale_accepted,
        decision_ms=decision_ms,
        operation_ms=operation_ms,
        trial_ms=_ms(started),
        error=error,
    )


async def _paired_trial(engine: AsyncEngine, window: FaultWindow, trial: int, seed: int) -> list[ComparisonSample]:
    """One snapshot, three independent resets, deterministic randomized ordering."""
    strategies = list(Strategy)
    random.Random(f"{seed}:{window.name}:{trial}").shuffle(strategies)
    async with engine.connect() as connection:
        created = False
        samples: list[ComparisonSample] = []
        try:
            await _create_temporary_ledger(connection)
            created = True
            for strategy in strategies:
                samples.append(await _one_policy(connection, strategy, window, trial))
        finally:
            # On connection-pool return, session TEMP tables otherwise persist.
            # Never drop an unqualified permanent table.
            if created:
                try:
                    await connection.rollback()
                    async with connection.begin():
                        await connection.execute(text("DROP TABLE IF EXISTS pg_temp.er_5c2_effects"))
                        await connection.execute(text("DROP TABLE IF EXISTS pg_temp.er_5c2_stage"))
                except Exception:
                    # Invalid cleanup means the trial cannot be reported valid.
                    samples = [
                        ComparisonSample(
                            strategy=strategy.value,
                            window=window.name,
                            trial=trial,
                            action=None,
                            artifacts_before=int(window.artifact_committed),
                            artifacts_after=None,
                            duplicated_effects=0,
                            missing_artifact_skips=0,
                            unproven_next_dispatches=0,
                            stale_write_attempts=int(window.old_worker_attempt),
                            stale_write_accepted=0,
                            decision_ms=None,
                            operation_ms=None,
                            trial_ms=0,
                            error="CleanupFailed",
                        )
                        for strategy in strategies
                    ]
        return samples


async def run_postgres_comparison(config: ComparisonConfig) -> tuple[list[ComparisonSample], float]:
    """Explicit opt-in, local PostgreSQL-only model experiment with no permanent writes."""
    if (
        os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
        or os.getenv("ENTERPRISE_RESEARCH_STRATEGY_COMPARISON") != "1"
    ):
        raise PermissionError("5C-2 requires both explicit comparison opt-in environment flags.")
    settings = PostgresSettings.from_env()
    if settings.host not in LOCAL_HOSTS or settings.port != 5432 or settings.database != "enterprise_research":
        raise PermissionError("5C-2 requires the local enterprise_research development database on port 5432.")
    engine = build_async_engine(settings)
    try:
        async with engine.connect() as connection:
            actual = await connection.scalar(select(func.current_database()))
            if actual != "enterprise_research":
                raise PermissionError("Unexpected PostgreSQL database; comparison aborted.")

        for trial in range(config.warmup_per_window):
            warmups: list[ComparisonSample] = []
            for window in FAULT_WINDOWS:
                warmups.extend(await asyncio.wait_for(_paired_trial(engine, window, trial, config.seed), timeout=20))
            validated = summarize_comparison(warmups, duration_s=1, trials_per_window=1)
            if not validated["experiment_valid"]:
                raise RuntimeError("Warmup did not match model or guarded safety contracts.")

        work = [(window, trial) for trial in range(config.trials_per_window) for window in FAULT_WINDOWS]
        random.Random(config.seed).shuffle(work)
        semaphore = asyncio.Semaphore(config.concurrency)

        async def limited(window: FaultWindow, trial: int) -> list[ComparisonSample]:
            async with semaphore:
                return await asyncio.wait_for(_paired_trial(engine, window, trial, config.seed), timeout=20)

        start = time.perf_counter()
        groups = await asyncio.gather(*(limited(window, trial) for window, trial in work))
        elapsed_s = time.perf_counter() - start
        samples = [sample for group in groups for sample in group]
        return samples, elapsed_s
    finally:
        await engine.dispose()
