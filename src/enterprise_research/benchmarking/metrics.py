"""Strict, dependency-free accounting for recovery benchmark observations.

Counts and denominators are explicit.  A safe PAUSED decision is not a
recovery failure; a successful resume means *dispatch*, not completed research.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = "3E-6D-5C.1"
READY = "ready_for_explicit_claim"
PAUSED = "paused_for_reconciliation"


@dataclass(frozen=True, slots=True)
class RecoveryBenchmarkSample:
    """One measured, isolated trial; all durations use monotonic wall time."""

    scenario: str
    trial: int
    expected_ready: bool | None
    observed_disposition: str | None
    dispatch_count: int
    stale_write_attempts: int
    stale_write_accepted: int
    inspect_ms: float | None
    decision_ms: float | None
    lock_claim_ms: float | None
    trial_ms: float
    task_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.scenario or isinstance(self.trial, bool) or not isinstance(self.trial, int) or self.trial < 0:
            raise ValueError("Scenario must be nonempty and trial must be nonnegative.")
        if self.expected_ready is not None and not isinstance(self.expected_ready, bool):
            raise ValueError("expected_ready must be a bool or None.")
        if self.observed_disposition not in (None, READY, PAUSED):
            raise ValueError("Unexpected resume disposition.")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.dispatch_count, self.stale_write_attempts, self.stale_write_accepted)
        ):
            raise ValueError("Counters must be nonnegative integers.")
        if self.stale_write_accepted > self.stale_write_attempts:
            raise ValueError("Accepted stale writes cannot exceed attempts.")
        for duration in (self.inspect_ms, self.decision_ms, self.lock_claim_ms, self.trial_ms):
            if duration is not None and (not math.isfinite(duration) or duration < 0):
                raise ValueError("Durations must be finite nonnegative milliseconds.")
        if self.error is not None and (not self.error or "\n" in self.error):
            raise ValueError("Error must be a nonempty one-line exception type.")

    @property
    def unauthorized_dispatches(self) -> int:
        """Unexpected stage dispatches (not mere repeated inspection calls)."""
        if self.expected_ready is True:
            return max(0, self.dispatch_count - 1)
        return self.dispatch_count


def nearest_rank(values: list[float], quantile: float) -> float | None:
    """Exact nearest-rank pXX (small N: high percentiles often equal max)."""
    if not 0 < quantile <= 1:
        raise ValueError("Quantile must be in (0, 1].")
    if not values:
        return None
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Percentiles need finite nonnegative samples.")
    ordered = sorted(values)
    return ordered[math.ceil(quantile * len(ordered)) - 1]


def latency_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "p50_ms": nearest_rank(values, 0.50),
        "p95_ms": nearest_rank(values, 0.95),
        "p99_ms": nearest_rank(values, 0.99),
        "max_ms": max(values, default=None),
    }


def summarize(samples: list[RecoveryBenchmarkSample], *, duration_s: float) -> dict[str, object]:
    """Do not hide invalid trials, missing denominators, or violated invariants."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("Batch duration must be positive and finite.")
    eligible = [sample for sample in samples if sample.expected_ready is True]
    must_pause = [sample for sample in samples if sample.expected_ready is False]
    resumption_trials = eligible + must_pause
    eligible_succeeded = [
        sample
        for sample in eligible
        if sample.observed_disposition == READY and sample.dispatch_count == 1 and sample.error is None
    ]
    correctly_paused = [
        sample
        for sample in must_pause
        if sample.observed_disposition == PAUSED and sample.dispatch_count == 0 and sample.error is None
    ]
    expected_lock = [sample for sample in samples if sample.expected_ready is None]
    lock_success = [sample for sample in expected_lock if sample.lock_claim_ms is not None and sample.error is None]
    unsafe = sum(sample.unauthorized_dispatches for sample in samples)
    stale_attempts = sum(sample.stale_write_attempts for sample in samples)
    stale_accepted = sum(sample.stale_write_accepted for sample in samples)
    failed = sum(sample.error is not None for sample in samples)
    success_rate = len(eligible_succeeded) / len(eligible) if eligible else None
    paused_rate = len(correctly_paused) / len(must_pause) if must_pause else None
    unsafe_rate = unsafe / len(resumption_trials) if resumption_trials else None
    fencing_rate = stale_accepted / stale_attempts if stale_attempts else None
    complete = (
        len(eligible_succeeded) == len(eligible)
        and len(correctly_paused) == len(must_pause)
        and len(lock_success) == len(expected_lock)
        and failed == 0
        and unsafe == 0
        and stale_accepted == 0
        and bool(samples)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {
            "trials": len(samples),
            "recovery_trials": len(resumption_trials),
            "eligible_trials": len(eligible),
            "eligible_dispatched": len(eligible_succeeded),
            "must_pause_trials": len(must_pause),
            "correctly_paused": len(correctly_paused),
            "lock_trials": len(expected_lock),
            "successful_lock_trials": len(lock_success),
            "unauthorized_dispatches": unsafe,
            "stale_write_attempts": stale_attempts,
            "stale_write_accepted": stale_accepted,
            "error_trials": failed,
        },
        "rates": {
            "safe_resume_dispatch_rate": success_rate,
            "correct_pause_rate": paused_rate,
            "unauthorized_dispatches_per_recovery_trial": unsafe_rate,
            "accepted_stale_writes_per_attempt": fencing_rate,
        },
        "latency": {
            "eligible_dispatch": latency_summary(
                [sample.decision_ms for sample in eligible_succeeded if sample.decision_ms is not None]
            ),
            "pause_decision": latency_summary(
                [sample.decision_ms for sample in correctly_paused if sample.decision_ms is not None]
            ),
            "read_only_inspection": latency_summary(
                [sample.inspect_ms for sample in samples if sample.inspect_ms is not None and sample.error is None]
            ),
            "contended_claim_roundtrip": latency_summary(
                [sample.lock_claim_ms for sample in lock_success if sample.lock_claim_ms is not None]
            ),
        },
        "batch": {
            "duration_s": duration_s,
            "trial_throughput_per_s_including_setup_cleanup": len(samples) / duration_s,
        },
        "all_invariants_passed": complete,
    }


def write_report(
    output_dir: Path,
    *,
    samples: list[RecoveryBenchmarkSample],
    duration_s: float,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Create an immutable report directory; never overwrite existing results."""
    summary = summarize(samples, duration_s=duration_s)
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "summary": summary,
        "samples": [asdict(s) for s in samples],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    columns = list(RecoveryBenchmarkSample.__dataclass_fields__)
    with (output_dir / "samples.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for sample in samples:
            writer.writerow(asdict(sample))
    counts = summary["counts"]
    rates = summary["rates"]
    latency = summary["latency"]

    def fmt_rate(name: str) -> str:
        value = rates[name]
        return "N/A (zero denominator)" if value is None else f"{value:.2%}"

    def fmt_latency(name: str) -> str:
        values = latency[name]
        if not values["n"]:
            return "N/A"
        return f"{values['p50_ms']:.2f} / {values['p95_ms']:.2f} / {values['p99_ms']:.2f} ms (n={values['n']})"

    lines = [
        "# 3E-6D-5C — Recovery Benchmark Report",
        "",
        "> Scope: isolated PostgreSQL Run/Journal/Plan benchmark; no LLM/HTTP, no full research completion.",
        "> This benchmark does not compare alternative recovery strategies.",
        "",
        f"- Recorded trials: **{counts['trials']}**",
        (
            f"- Eligible dispatch success: **{counts['eligible_dispatched']}/{counts['eligible_trials']}** "
            f"({fmt_rate('safe_resume_dispatch_rate')})"
        ),
        (
            f"- Correct conservative pauses: **{counts['correctly_paused']}/{counts['must_pause_trials']}** "
            f"({fmt_rate('correct_pause_rate')})"
        ),
        (
            f"- Unauthorized dispatches / recovery trial: "
            f"**{counts['unauthorized_dispatches']}/{counts['recovery_trials']}** "
            f"({fmt_rate('unauthorized_dispatches_per_recovery_trial')})"
        ),
        (
            f"- Accepted stale writes / attempted: "
            f"**{counts['stale_write_accepted']}/{counts['stale_write_attempts']}** "
            f"({fmt_rate('accepted_stale_writes_per_attempt')})"
        ),
        f"- Trial errors: **{counts['error_trials']}**",
        f"- Invariants passed: **{summary['all_invariants_passed']}**",
        "",
        "## Latencies (nearest-rank P50 / P95 / P99)",
        "",
        f"- Authorized stage dispatch: {fmt_latency('eligible_dispatch')}",
        f"- Safe pause decision: {fmt_latency('pause_decision')}",
        f"- Read-only reconciliation: {fmt_latency('read_only_inspection')}",
        f"- Row-lock-contended claim roundtrip: {fmt_latency('contended_claim_roundtrip')}",
        "",
        (
            f"Batch runtime: {duration_s:.3f}s; workload throughput (including fixture creation and cleanup): "
            f"{summary['batch']['trial_throughput_per_s_including_setup_cleanup']:.2f} trials/s."
        ),
        (
            "This throughput is **not** end-to-end research task throughput; "
            "lock roundtrip includes client/SQL scheduling overhead."
        ),
        "Warmups are excluded from measurements. Low sample sizes make tail percentiles unstable.",
        "See report.json and samples.csv for raw numbers, denominators and errors.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
