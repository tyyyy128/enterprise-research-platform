"""5C pure accounting tests: denominators, safety failures, tails and export."""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from enterprise_research.benchmarking.metrics import (
    PAUSED,
    READY,
    RecoveryBenchmarkSample,
    latency_summary,
    nearest_rank,
    summarize,
    write_report,
)
from enterprise_research.benchmarking.postgres_harness import BenchmarkConfig, run_postgres_benchmark


def sample(
    scenario: str,
    expected: bool | None,
    disposition: str | None,
    *,
    dispatches: int = 0,
    stale_attempts: int = 0,
    stale_accepted: int = 0,
    elapsed_ms: float | None = 5,
    lock_ms: float | None = None,
    error: str | None = None,
) -> RecoveryBenchmarkSample:
    return RecoveryBenchmarkSample(
        scenario=scenario,
        trial=0,
        expected_ready=expected,
        observed_disposition=disposition,
        dispatch_count=dispatches,
        stale_write_attempts=stale_attempts,
        stale_write_accepted=stale_accepted,
        inspect_ms=2 if expected is not None else None,
        decision_ms=elapsed_ms if expected is not None else None,
        lock_claim_ms=lock_ms,
        trial_ms=12,
        error=error,
    )


def test_safety_denominators_do_not_treat_correct_pauses_as_recovery_failures():
    rows = [
        sample("ready", True, READY, dispatches=1, stale_attempts=1),
        sample("started", False, PAUSED),
        sample("finished_no_plan", False, PAUSED),
        sample("lock", None, None, elapsed_ms=None, lock_ms=30),
    ]
    summary = summarize(rows, duration_s=2)
    assert summary["counts"] == {
        "trials": 4,
        "recovery_trials": 3,
        "eligible_trials": 1,
        "eligible_dispatched": 1,
        "must_pause_trials": 2,
        "correctly_paused": 2,
        "lock_trials": 1,
        "successful_lock_trials": 1,
        "unauthorized_dispatches": 0,
        "stale_write_attempts": 1,
        "stale_write_accepted": 0,
        "error_trials": 0,
    }
    assert summary["rates"]["safe_resume_dispatch_rate"] == 1
    assert summary["rates"]["correct_pause_rate"] == 1
    assert summary["rates"]["accepted_stale_writes_per_attempt"] == 0
    assert summary["batch"]["trial_throughput_per_s_including_setup_cleanup"] == 2
    assert summary["all_invariants_passed"] is True


def test_failures_are_visible_and_disqualify_benchmark_pass():
    rows = [
        sample("ready", True, READY, dispatches=2, stale_attempts=2, stale_accepted=1),
        sample("started", False, READY, dispatches=1),
        sample("lock", None, None, lock_ms=None, error="TimeoutError"),
    ]
    summary = summarize(rows, duration_s=1)
    assert summary["counts"]["unauthorized_dispatches"] == 2
    assert summary["rates"]["unauthorized_dispatches_per_recovery_trial"] == 1
    assert summary["rates"]["accepted_stale_writes_per_attempt"] == 0.5
    assert summary["counts"]["error_trials"] == 1
    assert summary["all_invariants_passed"] is False


def test_zero_denominators_are_null_not_zero_percent_success():
    summary = summarize([sample("lock", None, None, elapsed_ms=None, lock_ms=21)], duration_s=1)
    assert summary["rates"] == {
        "safe_resume_dispatch_rate": None,
        "correct_pause_rate": None,
        "unauthorized_dispatches_per_recovery_trial": None,
        "accepted_stale_writes_per_attempt": None,
    }
    assert summary["latency"]["eligible_dispatch"]["p99_ms"] is None
    assert summarize([], duration_s=1)["all_invariants_passed"] is False


def test_nearest_rank_tail_uses_max_for_small_samples():
    assert nearest_rank([11, 3, 8], 0.5) == 8
    assert nearest_rank([11, 3, 8], 0.95) == 11
    assert nearest_rank([11, 3, 8], 0.99) == 11
    assert nearest_rank([], 0.5) is None
    assert latency_summary([3, 8, 11])["n"] == 3
    with pytest.raises(ValueError):
        nearest_rank([1], 0)


def test_invalid_inputs_never_produce_a_plausible_success_report():
    with pytest.raises(ValueError):
        sample("ready", True, READY, stale_attempts=0, stale_accepted=1)
    with pytest.raises(ValueError):
        sample("ready", True, READY, elapsed_ms=math.nan)
    with pytest.raises(ValueError):
        summarize([], duration_s=0)
    with pytest.raises(ValueError):
        BenchmarkConfig(trials_per_scenario=0)
    with pytest.raises(ValueError):
        BenchmarkConfig(concurrency=12)
    with pytest.raises(ValueError):
        BenchmarkConfig(lock_hold_ms=1)
    with pytest.raises(ValueError):
        BenchmarkConfig(seed=-1)


def test_exports_are_structured_and_refuse_overwrite(tmp_path):
    output = tmp_path / "immutable-benchmark"
    rows = [sample("ready", True, READY, dispatches=1, stale_attempts=1)]
    summary = write_report(output, samples=rows, duration_s=0.5, metadata={"git_head": "test"})
    exported = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert exported["metadata"]["git_head"] == "test"
    assert exported["summary"] == summary
    assert exported["samples"][0]["dispatch_count"] == 1
    assert (output / "samples.csv").read_text(encoding="utf-8").count("\n") == 2
    assert "end-to-end research task throughput" in (output / "report.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_report(output, samples=rows, duration_s=0.5, metadata={})


def test_postgres_harness_rejects_missing_opt_in_and_remote_hosts(monkeypatch):
    config = BenchmarkConfig(trials_per_scenario=1, warmup_per_scenario=0)
    monkeypatch.delenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS", raising=False)
    monkeypatch.delenv("ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK", raising=False)
    with pytest.raises(PermissionError, match="both explicit"):
        asyncio.run(run_postgres_benchmark(config))

    monkeypatch.setenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS", "1")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK", "1")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_HOST", "db.example.invalid")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_PORT", "5432")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_NAME", "enterprise_research")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_USER", "test")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_PASSWORD", "not-a-real-password")
    with pytest.raises(PermissionError, match="local enterprise_research"):
        asyncio.run(run_postgres_benchmark(config))
