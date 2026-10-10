"""Explicit opt-in, real PostgreSQL smoke for the 5C-3 child-CLI suite."""

from __future__ import annotations

import os

import pytest

from enterprise_research.benchmarking.statistical_suite import SuitePlan
from enterprise_research.benchmarking.suite_cli import analyze_verified_suite, collect_suite

pytestmark = pytest.mark.skipif(
    any(
        os.getenv(flag) != "1"
        for flag in (
            "ENTERPRISE_RESEARCH_RUN_DB_TESTS",
            "ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK",
            "ENTERPRISE_RESEARCH_STRATEGY_COMPARISON",
            "ENTERPRISE_RESEARCH_STATISTICAL_SUITE",
        )
    ),
    reason="5C-3 requires four opt-ins and a migrated LOCAL development PostgreSQL database.",
)


def test_real_postgres_cross_cli_suite_smoke(tmp_path):
    plan = SuitePlan(seeds=(42,), concurrencies=(1,), repetitions=1, trials=1, warmup=0)
    root = tmp_path / "5c3-verified-suite"
    summary = collect_suite(root, plan)
    assert summary["run_count"] == 2
    assert summary["real_resume_by_concurrency"]["1"]["counts"]["trials"] == 4
    assert summary["synthetic_models_by_concurrency"]["1"]["strategy_cells"] == 30
    assert (
        summary["synthetic_models_by_concurrency"]["1"]["strategies"]["journal_artifact_fencing_model"]["unsafe_trials"]
        == 0
    )
    reviewed, records = analyze_verified_suite(root)
    assert reviewed == summary
    assert len(records) == 2
    assert (root / "analysis/report.md").is_file()
