"""5C-2: explicit *models* of three recovery policies, not production adapters.

All three policies receive the same synthetic PLANNING-stage failure snapshot.
PostgreSQL benchmarking persists that snapshot and its effect log in TEMP tables;
this module is deliberately deterministic and database independent.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from enterprise_research.benchmarking.metrics import latency_summary

SCHEMA_VERSION = "3E-6D-5C.2"


class Strategy(StrEnum):
    NAIVE_RETRY = "naive_retry_model"
    CHECKPOINT_ONLY = "checkpoint_only_model"
    GUARDED = "journal_artifact_fencing_model"


class Action(StrEnum):
    RETRY_STAGE = "retry_stage"
    DISPATCH_NEXT = "dispatch_next"
    PAUSE = "pause"


class Checkpoint(StrEnum):
    STALE = "stale"
    ADVANCED = "advanced"
    UNAVAILABLE = "unavailable"
    IDENTITY_CONFLICT = "identity_conflict"


@dataclass(frozen=True, slots=True)
class FaultWindow:
    name: str
    artifact_committed: bool
    journal_completed: bool
    checkpoint: Checkpoint
    old_worker_attempt: bool = False
    journal_attempts: int = 1

    def __post_init__(self) -> None:
        if (
            not self.name
            or not isinstance(self.artifact_committed, bool)
            or not isinstance(self.journal_completed, bool)
        ):
            raise ValueError("Invalid fault window definition.")
        if not isinstance(self.checkpoint, Checkpoint) or not isinstance(self.old_worker_attempt, bool):
            raise ValueError("Invalid checkpoint or worker condition.")
        if (
            isinstance(self.journal_attempts, bool)
            or not isinstance(self.journal_attempts, int)
            or self.journal_attempts < 0
        ):
            raise ValueError("Invalid journal attempt count.")

    @property
    def proven_for_next(self) -> bool:
        return (
            self.journal_completed
            and self.journal_attempts == 1
            and self.artifact_committed
            and self.checkpoint not in {Checkpoint.UNAVAILABLE, Checkpoint.IDENTITY_CONFLICT}
        )


FAULT_WINDOWS = (
    FaultWindow("before_effect", False, False, Checkpoint.STALE),
    FaultWindow("effect_before_journal", True, False, Checkpoint.STALE),
    FaultWindow("effect_after_checkpoint", True, False, Checkpoint.ADVANCED),
    FaultWindow("completed_checkpoint_stale", True, True, Checkpoint.STALE),
    FaultWindow("completed_checkpoint_advanced", True, True, Checkpoint.ADVANCED),
    FaultWindow("completed_missing_artifact", False, True, Checkpoint.ADVANCED),
    FaultWindow("checkpoint_unavailable", True, True, Checkpoint.UNAVAILABLE),
    FaultWindow("checkpoint_identity_conflict", True, True, Checkpoint.IDENTITY_CONFLICT),
    FaultWindow("duplicate_journal_attempts", True, True, Checkpoint.ADVANCED, journal_attempts=2),
    FaultWindow("stale_worker_after_takeover", True, True, Checkpoint.ADVANCED, old_worker_attempt=True),
)
WINDOW_BY_NAME = {window.name: window for window in FAULT_WINDOWS}


@dataclass(frozen=True, slots=True)
class Observation:
    """Observation read by a policy (unused fields are not exposed to it)."""

    checkpoint: Checkpoint | None = None
    journal_completed: bool | None = None
    artifact_count: int | None = None
    journal_attempts: int | None = None


def decide(strategy: Strategy, observation: Observation) -> Action:
    """Defined experimental policies; deliberately not a production retry implementation."""
    if strategy == Strategy.NAIVE_RETRY:
        return Action.RETRY_STAGE
    if not isinstance(observation.checkpoint, Checkpoint):
        raise ValueError("Checkpoint observation is required for this policy.")
    if strategy == Strategy.CHECKPOINT_ONLY:
        if observation.checkpoint in (Checkpoint.UNAVAILABLE, Checkpoint.IDENTITY_CONFLICT):
            return Action.PAUSE
        return Action.DISPATCH_NEXT if observation.checkpoint == Checkpoint.ADVANCED else Action.RETRY_STAGE
    if strategy != Strategy.GUARDED:
        raise ValueError("Unsupported strategy.")
    if observation.checkpoint in (Checkpoint.UNAVAILABLE, Checkpoint.IDENTITY_CONFLICT):
        return Action.PAUSE
    if observation.journal_completed is True and observation.journal_attempts == 1 and observation.artifact_count == 1:
        return Action.DISPATCH_NEXT
    return Action.PAUSE


@dataclass(frozen=True, slots=True)
class ComparisonSample:
    strategy: str
    window: str
    trial: int
    action: str | None
    artifacts_before: int
    artifacts_after: int | None
    duplicated_effects: int
    missing_artifact_skips: int
    unproven_next_dispatches: int
    stale_write_attempts: int
    stale_write_accepted: int
    decision_ms: float | None
    operation_ms: float | None
    trial_ms: float
    error: str | None = None

    def __post_init__(self) -> None:
        if self.strategy not in Strategy._value2member_map_ or self.window not in WINDOW_BY_NAME:
            raise ValueError("Unknown strategy or failure window.")
        if isinstance(self.trial, bool) or not isinstance(self.trial, int) or self.trial < 0:
            raise ValueError("Invalid trial number.")
        if self.action is not None and self.action not in Action._value2member_map_:
            raise ValueError("Unknown action.")
        for number in (
            self.artifacts_before,
            self.duplicated_effects,
            self.missing_artifact_skips,
            self.unproven_next_dispatches,
            self.stale_write_attempts,
            self.stale_write_accepted,
        ):
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ValueError("Invalid nonnegative counter.")
        if self.artifacts_after is not None and (
            isinstance(self.artifacts_after, bool)
            or not isinstance(self.artifacts_after, int)
            or self.artifacts_after < 0
        ):
            raise ValueError("Invalid artifact result.")
        if self.stale_write_accepted > self.stale_write_attempts:
            raise ValueError("Stale accepted count exceeds attempts.")
        for latency in (self.decision_ms, self.operation_ms, self.trial_ms):
            if latency is not None and (not math.isfinite(latency) or latency < 0):
                raise ValueError("Latency must be finite and nonnegative.")
        if self.error is not None and (not self.error or "\n" in self.error):
            raise ValueError("Error must be a single exception type.")

    @property
    def violated_safety(self) -> bool:
        return any(
            (
                self.duplicated_effects,
                self.missing_artifact_skips,
                self.unproven_next_dispatches,
                self.stale_write_accepted,
            )
        )


def expected_counts(strategy: Strategy, window: FaultWindow) -> tuple[Action, int, int, int, int, int]:
    """Fixture accounting oracle given the declared policy, not an empirical risk estimate.

    Action, resulting artifact count, duplicates, missing skip, unsupported
    next dispatch, and accepted stale write.
    """
    action = decide(
        strategy,
        Observation(
            checkpoint=window.checkpoint,
            journal_completed=window.journal_completed,
            artifact_count=int(window.artifact_committed),
            journal_attempts=window.journal_attempts,
        ),
    )
    after = int(window.artifact_committed) + int(action == Action.RETRY_STAGE)
    duplicates = max(0, after - 1)
    missing = int(action == Action.DISPATCH_NEXT and after == 0)
    unproven = int(action == Action.DISPATCH_NEXT and not window.proven_for_next)
    stale_accepted = int(window.old_worker_attempt and strategy != Strategy.GUARDED)
    return action, after, duplicates, missing, unproven, stale_accepted


def summarize_comparison(samples: list[ComparisonSample], *, duration_s: float, trials_per_window: int) -> dict:
    """Keep expected baseline hazards visible; reject errors or model drift."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("Duration must be positive and finite.")
    if isinstance(trials_per_window, bool) or not isinstance(trials_per_window, int) or trials_per_window < 1:
        raise ValueError("trials_per_window must be a positive integer.")
    expected_keys = {
        (strategy.value, window.name, trial)
        for strategy in Strategy
        for window in FAULT_WINDOWS
        for trial in range(trials_per_window)
    }
    observed_keys = [(s.strategy, s.window, s.trial) for s in samples]
    complete = len(observed_keys) == len(expected_keys) and set(observed_keys) == expected_keys
    no_duplicate_keys = len(observed_keys) == len(set(observed_keys))
    matches_model = True
    for sample in samples:
        window = WINDOW_BY_NAME[sample.window]
        action, effects, dup, missing, unproven, stale = expected_counts(Strategy(sample.strategy), window)
        if (
            sample.error is not None
            or sample.action != action.value
            or sample.artifacts_before != int(window.artifact_committed)
            or sample.artifacts_after != effects
            or sample.duplicated_effects != dup
            or sample.missing_artifact_skips != missing
            or sample.unproven_next_dispatches != unproven
            or sample.stale_write_attempts != int(window.old_worker_attempt)
            or sample.stale_write_accepted != stale
            or sample.decision_ms is None
            or sample.operation_ms is None
        ):
            matches_model = False
    by_strategy: dict[str, dict] = {}
    for strategy in Strategy:
        rows = [sample for sample in samples if sample.strategy == strategy.value]
        proven = [sample for sample in rows if WINDOW_BY_NAME[sample.window].proven_for_next]
        advances = [sample for sample in rows if sample.action == Action.DISPATCH_NEXT]
        stale_attempts = sum(sample.stale_write_attempts for sample in rows)
        safety_failures = sum(sample.violated_safety for sample in rows)
        values = {
            "trials": len(rows),
            "retries": sum(sample.action == Action.RETRY_STAGE for sample in rows),
            "next_dispatches": len(advances),
            "pauses": sum(sample.action == Action.PAUSE for sample in rows),
            "proven_next_opportunities": len(proven),
            "proven_next_dispatched": sum(sample.action == Action.DISPATCH_NEXT for sample in proven),
            "duplicate_effect_trials": sum(sample.duplicated_effects > 0 for sample in rows),
            "duplicate_effects": sum(sample.duplicated_effects for sample in rows),
            "missing_artifact_skips": sum(sample.missing_artifact_skips for sample in rows),
            "unproven_next_dispatches": sum(sample.unproven_next_dispatches for sample in rows),
            "stale_write_attempts": stale_attempts,
            "stale_write_accepted": sum(sample.stale_write_accepted for sample in rows),
            "trials_with_safety_violations": safety_failures,
            "error_trials": sum(sample.error is not None for sample in rows),
        }
        by_strategy[strategy.value] = {
            "counts": values,
            "rates": {
                "trials_with_safety_violations": safety_failures / len(rows) if rows else None,
                "proven_next_dispatched": values["proven_next_dispatched"] / len(proven) if proven else None,
                "unproven_next_per_dispatch": (
                    values["unproven_next_dispatches"] / len(advances) if advances else None
                ),
                "stale_write_acceptance": (values["stale_write_accepted"] / stale_attempts if stale_attempts else None),
            },
            "latency": {
                "decision": latency_summary([sample.decision_ms for sample in rows if sample.decision_ms is not None]),
                "operation": latency_summary(
                    [sample.operation_ms for sample in rows if sample.operation_ms is not None]
                ),
            },
        }
    guarded = by_strategy[Strategy.GUARDED.value]["counts"]
    guarded_safe = guarded["trials_with_safety_violations"] == 0 and guarded["error_trials"] == 0
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment": "paired_synthetic_policies_postgres_temp_tables_not_production_executor",
        "samples": len(samples),
        "expected_samples": len(expected_keys),
        "paired_workload_complete": complete and no_duplicate_keys,
        "observations_match_defined_models": matches_model,
        "guarded_model_safe": guarded_safe,
        "experiment_valid": complete and no_duplicate_keys and matches_model and guarded_safe,
        "batch_duration_s_including_setup_cleanup": duration_s,
        "workload_trials_per_second": len(samples) / duration_s,
        "strategies": by_strategy,
    }


def write_comparison_report(
    output_dir: Path,
    *,
    samples: list[ComparisonSample],
    duration_s: float,
    trials_per_window: int,
    metadata: dict,
) -> dict:
    """Immutable directory with raw observations, report and explicit model disclaimer."""
    summary = summarize_comparison(samples, duration_s=duration_s, trials_per_window=trials_per_window)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "report.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "metadata": metadata,
                "summary": summary,
                "samples": [asdict(sample) for sample in samples],
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    with (output_dir / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ComparisonSample.__dataclass_fields__))
        writer.writeheader()
        for sample in samples:
            writer.writerow(asdict(sample))

    def fmt(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.2%}"

    lines = [
        "# 3E-6D-5C-2 — Recovery Strategy Model Comparison",
        "",
        "> **MODELED policies**, not an empirical comparison of three production implementations.",
        "> Same PLANNING fault snapshots and PostgreSQL session-local TEMP ledger; no real Research Runs or LLM calls.",
        "> Guarded policy is a simplified model, **not** ResearchRunExecutor.resume().",
        "> 5C-1 separately exercises real Safe Resume; its latencies are not comparable to these.",
        "",
        f"- Matched windows: {len(FAULT_WINDOWS)}; measured repetitions per window: {trials_per_window}",
        f"- Total measured strategy cells: {len(samples)}",
        f"- Complete paired matrix: **{summary['paired_workload_complete']}**",
        f"- Results agree with predeclared policy models: **{summary['observations_match_defined_models']}**",
        f"- Guarded model safety invariants: **{summary['guarded_model_safe']}**",
        f"- Overall experiment validity: **{summary['experiment_valid']}**",
        "",
        "## Observed effects and decisions (intentionally unsafe baselines are not hidden)",
        "",
        "| Model | Duplicated effects | Unproven advances | Missing skips | "
        "Stale accepted | Unsafe trials | Proven handoffs |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in Strategy:
        record = summary["strategies"][strategy.value]
        counts = record["counts"]
        lines.append(
            f"| {strategy.value} | {counts['duplicate_effects']} | {counts['unproven_next_dispatches']} | "
            f"{counts['missing_artifact_skips']} | {counts['stale_write_accepted']}/{counts['stale_write_attempts']} | "
            f"{counts['trials_with_safety_violations']}/{counts['trials']} | "
            f"{counts['proven_next_dispatched']}/{counts['proven_next_opportunities']} |"
        )
    lines.extend(
        [
            "",
            "## SQL-backed model timing (nearest-rank P50 / P95 / P99)",
            "",
            "| Strategy | Decision read (ms) | Policy + simulated effects + audit (ms) | Stale write acceptance |",
            "|---|---:|---:|---:|",
        ]
    )
    for strategy in Strategy:
        record = summary["strategies"][strategy.value]

        def timings(name: str) -> str:
            values = record["latency"][name]
            if values["n"] == 0:
                return "N/A"
            return f"{values['p50_ms']:.2f} / {values['p95_ms']:.2f} / {values['p99_ms']:.2f} (n={values['n']})"

        lines.append(
            f"| {strategy.value} | {timings('decision')} | {timings('operation')} | "
            f"{fmt(record['rates']['stale_write_acceptance'])} |"
        )
    lines.extend(
        [
            "",
            f"Batch duration: {duration_s:.3f}s; policy cells/s with setup and cleanup: "
            f"{summary['workload_trials_per_second']:.2f}.",
            "Different SQL operations per policy: descriptive times, NOT production speedup ratios.",
            "Synthetic baseline weaknesses show possible failure modes, not empirical incident rates.",
            "Small samples make P95/P99 unstable. Inspect raw report.json and samples.csv.",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
