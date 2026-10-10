"""5C-2: pure model contracts, fault windows, denominators, and report safety."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from enterprise_research.benchmarking.postgres_strategy_comparison import ComparisonConfig, run_postgres_comparison
from enterprise_research.benchmarking.strategy_comparison import (
    FAULT_WINDOWS,
    Action,
    Checkpoint,
    ComparisonSample,
    Observation,
    Strategy,
    decide,
    expected_counts,
    summarize_comparison,
    write_comparison_report,
)


def fixture_samples(trials: int = 1) -> list[ComparisonSample]:
    """Hand-independent oracle rows; no artificial delays are performance claims."""
    rows = []
    for window in FAULT_WINDOWS:
        for trial in range(trials):
            for strategy in Strategy:
                action, after, duplicates, missing, unproven, stale = expected_counts(strategy, window)
                rows.append(
                    ComparisonSample(
                        strategy=strategy.value,
                        window=window.name,
                        trial=trial,
                        action=action.value,
                        artifacts_before=int(window.artifact_committed),
                        artifacts_after=after,
                        duplicated_effects=duplicates,
                        missing_artifact_skips=missing,
                        unproven_next_dispatches=unproven,
                        stale_write_attempts=int(window.old_worker_attempt),
                        stale_write_accepted=stale,
                        decision_ms=1.5,
                        operation_ms=2.0,
                        trial_ms=3.0,
                    )
                )
    return rows


def test_predeclared_fault_windows_contain_safe_and_unsafe_replay_cases():
    assert len(FAULT_WINDOWS) == 10
    assert len({w.name for w in FAULT_WINDOWS}) == 10
    assert sum(w.old_worker_attempt for w in FAULT_WINDOWS) == 1
    assert any(w.checkpoint == Checkpoint.IDENTITY_CONFLICT for w in FAULT_WINDOWS)
    assert any(w.journal_completed and not w.artifact_committed for w in FAULT_WINDOWS)
    assert any(not w.journal_completed and w.artifact_committed for w in FAULT_WINDOWS)
    assert any(w.journal_attempts > 1 for w in FAULT_WINDOWS)


def test_strategy_contracts_are_explicit_and_conservative():
    before = Observation(Checkpoint.STALE, False, 0)
    assert decide(Strategy.NAIVE_RETRY, before) == Action.RETRY_STAGE
    assert decide(Strategy.CHECKPOINT_ONLY, before) == Action.RETRY_STAGE
    assert decide(Strategy.GUARDED, before) == Action.PAUSE
    assert decide(Strategy.CHECKPOINT_ONLY, Observation(Checkpoint.ADVANCED)) == Action.DISPATCH_NEXT
    assert decide(Strategy.GUARDED, Observation(Checkpoint.ADVANCED, True, 0, 1)) == Action.PAUSE
    assert decide(Strategy.GUARDED, Observation(Checkpoint.ADVANCED, False, 1, 1)) == Action.PAUSE
    assert decide(Strategy.GUARDED, Observation(Checkpoint.STALE, True, 1, 1)) == Action.DISPATCH_NEXT
    assert decide(Strategy.GUARDED, Observation(Checkpoint.ADVANCED, True, 1, 2)) == Action.PAUSE
    assert decide(Strategy.GUARDED, Observation(Checkpoint.IDENTITY_CONFLICT, True, 1, 1)) == Action.PAUSE
    assert decide(Strategy.GUARDED, Observation(Checkpoint.UNAVAILABLE, True, 1, 1)) == Action.PAUSE
    with pytest.raises(ValueError):
        decide(Strategy.GUARDED, Observation())


def test_expected_model_exposure_is_explicit_not_hidden_as_success():
    summary = summarize_comparison(fixture_samples(), duration_s=1.0, trials_per_window=1)
    assert summary["experiment_valid"] is True
    assert summary["samples"] == 30
    a = summary["strategies"][Strategy.NAIVE_RETRY.value]["counts"]
    b = summary["strategies"][Strategy.CHECKPOINT_ONLY.value]["counts"]
    c = summary["strategies"][Strategy.GUARDED.value]["counts"]
    assert (a["duplicate_effects"], a["stale_write_accepted"]) == (8, 1)
    assert (b["duplicate_effects"], b["missing_artifact_skips"], b["unproven_next_dispatches"]) == (2, 1, 3)
    assert b["stale_write_accepted"] == 1
    assert c["duplicate_effects"] == c["unproven_next_dispatches"] == c["stale_write_accepted"] == 0
    assert c["proven_next_dispatched"] == 3
    assert c["proven_next_opportunities"] == 3
    assert a["trials_with_safety_violations"] == 8
    assert c["trials_with_safety_violations"] == 0
    assert summary["strategies"][Strategy.GUARDED.value]["rates"]["stale_write_acceptance"] == 0


def test_broken_guard_and_incomplete_or_duplicate_matrix_fail_validation():
    rows = fixture_samples()
    guarded = next(i for i, item in enumerate(rows) if item.strategy == Strategy.GUARDED.value)
    contaminated = rows.copy()
    contaminated[guarded] = replace(contaminated[guarded], stale_write_accepted=1, stale_write_attempts=1)
    assert summarize_comparison(contaminated, duration_s=1, trials_per_window=1)["experiment_valid"] is False
    assert summarize_comparison(rows[:-1], duration_s=1, trials_per_window=1)["experiment_valid"] is False
    assert summarize_comparison(rows + rows[:1], duration_s=1, trials_per_window=1)["experiment_valid"] is False
    error_rows = rows.copy()
    error_rows[guarded] = replace(error_rows[guarded], error="DriverError")
    assert summarize_comparison(error_rows, duration_s=1, trials_per_window=1)["experiment_valid"] is False


def test_validation_catches_bad_numbers_and_invalid_config():
    rows = fixture_samples()
    with pytest.raises(ValueError):
        summarize_comparison(rows, duration_s=0, trials_per_window=1)
    with pytest.raises(ValueError):
        summarize_comparison(rows, duration_s=1, trials_per_window=0)
    with pytest.raises(ValueError):
        replace(rows[0], operation_ms=float("nan"))
    with pytest.raises(ValueError):
        replace(rows[0], stale_write_attempts=0, stale_write_accepted=1)
    with pytest.raises(ValueError):
        ComparisonConfig(trials_per_window=0)
    with pytest.raises(ValueError):
        ComparisonConfig(concurrency=9)
    with pytest.raises(ValueError):
        ComparisonConfig(seed=-1)
    with pytest.raises(ValueError):
        ComparisonConfig(warmup_per_window=6)


def test_report_is_immutable_auditable_and_clearly_modeled(tmp_path):
    directory = tmp_path / "comparison"
    result = write_comparison_report(
        directory, samples=fixture_samples(), duration_s=2, trials_per_window=1, metadata={"git_head": "fixture"}
    )
    payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert payload["summary"] == result
    assert payload["metadata"]["git_head"] == "fixture"
    assert len(payload["samples"]) == 30
    assert (directory / "samples.csv").read_text(encoding="utf-8").count("\n") == 31
    text = (directory / "report.md").read_text(encoding="utf-8")
    assert "not an empirical comparison of three production implementations" in text
    assert "not comparable to these" in text
    assert "| naive_retry_model |" in text
    with pytest.raises(FileExistsError):
        write_comparison_report(directory, samples=fixture_samples(), duration_s=1, trials_per_window=1, metadata={})


def test_run_requires_explicit_flags_and_rejects_nonlocal_database(monkeypatch):
    config = ComparisonConfig(trials_per_window=1, warmup_per_window=0)
    monkeypatch.delenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS", raising=False)
    monkeypatch.delenv("ENTERPRISE_RESEARCH_STRATEGY_COMPARISON", raising=False)
    with pytest.raises(PermissionError, match="both explicit"):
        asyncio.run(run_postgres_comparison(config))

    monkeypatch.setenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS", "1")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_STRATEGY_COMPARISON", "1")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_HOST", "db.example.invalid")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_PORT", "5432")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_NAME", "enterprise_research")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_USER", "bench")
    monkeypatch.setenv("ENTERPRISE_RESEARCH_DB_PASSWORD", "test-only")
    with pytest.raises(PermissionError, match="local enterprise_research"):
        asyncio.run(run_postgres_comparison(config))
