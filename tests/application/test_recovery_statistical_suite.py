"""5C-3 offline and simulated-CLI tests; no database or LLM dependencies."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_research.benchmarking.metrics import (
    PAUSED,
    READY,
    RecoveryBenchmarkSample,
    write_report,
)
from enterprise_research.benchmarking.statistical_suite import (
    REPORT_FILES,
    SuitePlan,
    planned_runs,
    wilson_95,
    zero_event_upper_95,
)
from enterprise_research.benchmarking.strategy_comparison import (
    FAULT_WINDOWS,
    ComparisonSample,
    Strategy,
    expected_counts,
    write_comparison_report,
)
from enterprise_research.benchmarking.suite_cli import (
    analyze_verified_suite,
    collect_suite,
    main,
)


def _recovery_samples(plan: SuitePlan) -> list[RecoveryBenchmarkSample]:
    samples = []
    scenarios = (
        ("ready_two_workers", True, READY, 1),
        ("started_with_plan", False, PAUSED, 0),
        ("completed_without_plan", False, PAUSED, 0),
        ("contended_initial_claim", None, None, 0),
    )
    for scenario, ready, observed, dispatch_count in scenarios:
        for trial in range(plan.trials):
            samples.append(
                RecoveryBenchmarkSample(
                    scenario=scenario,
                    trial=trial,
                    expected_ready=ready,
                    observed_disposition=observed,
                    dispatch_count=dispatch_count,
                    stale_write_attempts=int(ready is True),
                    stale_write_accepted=0,
                    inspect_ms=9.0 if ready is not None else None,
                    decision_ms=(48 + trial) if ready is True else (9 + trial if ready is False else None),
                    lock_claim_ms=29 + trial if ready is None else None,
                    trial_ms=100.0 + trial,
                )
            )
    return samples


def _model_samples(plan: SuitePlan) -> list[ComparisonSample]:
    samples = []
    for window in FAULT_WINDOWS:
        for trial in range(plan.trials):
            for strategy in Strategy:
                action, after, duplicates, missing, unproven, accepted = expected_counts(strategy, window)
                samples.append(
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
                        stale_write_accepted=accepted,
                        decision_ms=0.5 + trial * 0.01,
                        operation_ms=1.2 + trial * 0.01,
                        trial_ms=2.0 + trial * 0.01,
                    )
                )
    return samples


def _fake_runner(spec, output: Path, plan: SuitePlan) -> None:
    common = {
        "git_head": "1cfa666",
        "concurrency": spec.concurrency,
        "seed": spec.seed,
    }
    if spec.kind == "recovery":
        write_report(
            output,
            samples=_recovery_samples(plan),
            duration_s=0.3,
            metadata={
                **common,
                "trials_per_scenario": plan.trials,
                "warmup_per_scenario": plan.warmup,
                "lock_hold_ms": plan.lock_hold_ms,
            },
        )
    else:
        write_comparison_report(
            output,
            samples=_model_samples(plan),
            duration_s=0.2,
            trials_per_window=plan.trials,
            metadata={**common, "trials_per_window": plan.trials, "warmup_per_window": plan.warmup},
        )


def _opt_in(monkeypatch):
    for name in (
        "ENTERPRISE_RESEARCH_RUN_DB_TESTS",
        "ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK",
        "ENTERPRISE_RESEARCH_STRATEGY_COMPARISON",
    ):
        monkeypatch.setenv(name, "1")


def test_suite_plan_rejects_unbounded_or_duplicated_workloads():
    with pytest.raises(ValueError):
        SuitePlan(seeds=(1, 1))
    with pytest.raises(ValueError):
        SuitePlan(seeds=(True, 2))
    with pytest.raises(ValueError):
        SuitePlan(concurrencies=(1, 5))
    with pytest.raises(ValueError):
        SuitePlan(seeds=(1, 2, 3, 4, 5), concurrencies=(1, 2, 3, 4), repetitions=3)
    with pytest.raises(ValueError):
        SuitePlan(trials=0)
    with pytest.raises(ValueError):
        SuitePlan(warmup=6)
    assert len(planned_runs(SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1))) == 2


def test_zero_event_upper_and_wilson_require_visible_denominators():
    assert zero_event_upper_95(0, 0) is None
    assert zero_event_upper_95(1, 10) is None
    assert zero_event_upper_95(0, 10) == pytest.approx(0.25886555)
    assert wilson_95(0, 0) is None
    lower, upper = wilson_95(10, 10)
    assert 0.70 < lower < 0.80
    assert upper == 1.0
    with pytest.raises(ValueError):
        zero_event_upper_95(2, 1)
    with pytest.raises(ValueError):
        wilson_95(11, 10)


def test_real_and_synthetic_reports_are_audited_and_never_pooled(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42, 43, 44), concurrencies=(1,), repetitions=2, trials=2, warmup=0)
    root = tmp_path / "suite01"
    summary = collect_suite(root, plan, runner=_fake_runner)
    assert summary["run_count"] == 12
    assert summary["git_head"] == "1cfa666"
    assert summary["real_resume_by_concurrency"]["1"]["counts"]["trials"] == 48
    assert summary["real_resume_by_concurrency"]["1"]["counts"]["error_trials"] == 0
    assert (
        summary["real_resume_by_concurrency"]["1"]["latency"]["eligible_dispatch"][
            "cluster_bootstrap_p50_interval95_ms"
        ]
        is not None
    )
    model = summary["synthetic_models_by_concurrency"]["1"]["strategies"]
    assert model[Strategy.NAIVE_RETRY.value]["unsafe_trials"] > 0
    assert model[Strategy.CHECKPOINT_ONLY.value]["unsafe_trials"] > 0
    assert model[Strategy.GUARDED.value]["unsafe_trials"] == 0
    assert (root / "manifest.json").is_file()
    assert (root / "analysis/report.json").is_file()
    for filename in REPORT_FILES:
        assert (root / planned_runs(plan)[0].directory / filename).is_file()
    assert "MODELED" not in (root / "analysis/report.md").read_text(encoding="utf-8")
    assert "合成策略模型" in (root / "analysis/report.md").read_text(encoding="utf-8")
    import xml.etree.ElementTree as ET

    assert ET.parse(root / "analysis/recovery_dispatch_p50.svg").getroot().tag.endswith("svg")
    assert ET.parse(root / "analysis/model_unsafe_trials.svg").getroot().tag.endswith("svg")
    checked, records = analyze_verified_suite(root)
    assert checked == summary
    assert len(records) == 12
    monkeypatch.delenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS")
    out = tmp_path / "offline-analysis"
    assert main(["analyze", "--suite-dir", str(root), "--output-dir", str(out)]) == 0
    assert (out / "metrics.csv").is_file()
    assert (out / "runs.csv").is_file()
    assert main(["analyze", "--suite-dir", str(root), "--output-dir", str(out)]) == 2
    _opt_in(monkeypatch)
    with pytest.raises(FileExistsError):
        collect_suite(root, plan, runner=_fake_runner)


def test_small_batch_marks_bootstrap_ci_unavailable(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42,), concurrencies=(1, 2), repetitions=1, trials=1, warmup=0)
    summary = collect_suite(tmp_path / "low-n", plan, runner=_fake_runner)
    for group in summary["real_resume_by_concurrency"].values():
        assert group["latency"]["eligible_dispatch"]["cluster_bootstrap_p50_interval95_ms"] is None
    assert summary["synthetic_models_by_concurrency"]["1"]["strategy_cells"] == 30


def test_modified_source_or_forged_summary_is_rejected(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1, trials=1, warmup=0)
    root = tmp_path / "suite"
    collect_suite(root, plan, runner=_fake_runner)
    spec = planned_runs(plan)[0]
    report = root / spec.directory / "report.json"
    raw = json.loads(report.read_text())
    raw["summary"]["counts"]["eligible_dispatched"] += 1
    report.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="checksum mismatch"):
        analyze_verified_suite(root)
    # A hash matching a doctored file still must not evade independent accounting.
    manifest = json.loads((root / "manifest.json").read_text())
    from enterprise_research.benchmarking.statistical_suite import sha256_file

    manifest["jobs"][0]["sha256"]["report.json"] = sha256_file(report)
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="independent safety and summary"):
        analyze_verified_suite(root)


def test_incomplete_suite_never_emits_conclusion(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1, trials=1, warmup=0)
    root = tmp_path / "partial"

    def fail_on_second(spec, output, plan):
        if spec.kind == "model":
            raise RuntimeError("simulated child failure")
        _fake_runner(spec, output, plan)

    with pytest.raises(RuntimeError, match="simulated"):
        collect_suite(root, plan, runner=fail_on_second)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["failed_job"] == "model/c1-s42-r01"
    assert not (root / "analysis").exists()
    with pytest.raises(ValueError, match="Incomplete suite"):
        analyze_verified_suite(root)


def test_failed_safety_invariant_cannot_be_summarized_as_valid(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1, trials=1, warmup=0)

    def bad_runner(spec, output, plan):
        if spec.kind == "recovery":
            rows = _recovery_samples(plan)
            rows[0] = replace(rows[0], stale_write_accepted=1)
            write_report(
                output,
                samples=rows,
                duration_s=0.3,
                metadata={
                    "git_head": "1cfa666",
                    "seed": spec.seed,
                    "concurrency": spec.concurrency,
                    "trials_per_scenario": plan.trials,
                    "warmup_per_scenario": plan.warmup,
                    "lock_hold_ms": plan.lock_hold_ms,
                },
            )
        else:
            _fake_runner(spec, output, plan)

    with pytest.raises(ValueError, match="independent safety"):
        collect_suite(tmp_path / "invalid", plan, runner=bad_runner)


def test_collection_requires_all_optins_but_offline_analysis_does_not(tmp_path, monkeypatch):
    for name in (
        "ENTERPRISE_RESEARCH_RUN_DB_TESTS",
        "ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK",
        "ENTERPRISE_RESEARCH_STRATEGY_COMPARISON",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PermissionError, match="all three"):
        collect_suite(tmp_path / "not-created", SuitePlan(), runner=_fake_runner)
    assert not (tmp_path / "not-created").exists()
    assert main(["collect", "--output-dir", str(tmp_path / "blocked")]) == 2
    assert not (tmp_path / "blocked").exists()


def test_mixed_git_revisions_abort_aggregate(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42, 43), concurrencies=(1,), repetitions=1, trials=1, warmup=0)

    def mixed_runner(spec, output, plan):
        _fake_runner(spec, output, plan)
        if spec.seed == 43:
            report = output / "report.json"
            data = json.loads(report.read_text(encoding="utf-8"))
            data["metadata"]["git_head"] = "abc1234"
            report.write_text(json.dumps(data), encoding="utf-8")

    root = tmp_path / "mixed"
    with pytest.raises(ValueError, match="different Git revisions"):
        collect_suite(root, plan, runner=mixed_runner)
    assert not (root / "analysis").exists()
    with pytest.raises(ValueError, match="different Git revisions"):
        analyze_verified_suite(root)


def test_manifest_rejects_source_path_symlink(tmp_path, monkeypatch):
    _opt_in(monkeypatch)
    plan = SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1, trials=1, warmup=0)
    root = tmp_path / "signed"
    collect_suite(root, plan, runner=_fake_runner)
    spec = planned_runs(plan)[0]
    file_path = root / spec.directory / "report.md"
    sibling = root / "copy.md"
    sibling.write_bytes(file_path.read_bytes())
    file_path.unlink()
    file_path.symlink_to(sibling)
    with pytest.raises(ValueError, match="Symlink"):
        analyze_verified_suite(root)
