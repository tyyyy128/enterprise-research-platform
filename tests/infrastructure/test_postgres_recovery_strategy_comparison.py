"""5C-2 paired model benchmark against session-local PostgreSQL TEMP tables."""

from __future__ import annotations

import asyncio
import os

import pytest

from enterprise_research.benchmarking.postgres_strategy_comparison import ComparisonConfig, run_postgres_comparison
from enterprise_research.benchmarking.strategy_comparison import FAULT_WINDOWS, Strategy, summarize_comparison

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_STRATEGY_COMPARISON") != "1",
    reason="5C-2 requires explicit opt-in and the local enterprise_research development PostgreSQL database.",
)


def test_postgresql_temp_table_strategy_comparison_smoke():
    samples, elapsed = asyncio.run(
        run_postgres_comparison(ComparisonConfig(trials_per_window=1, warmup_per_window=0, concurrency=2, seed=42))
    )
    summary = summarize_comparison(samples, duration_s=elapsed, trials_per_window=1)
    assert len(samples) == len(FAULT_WINDOWS) * len(Strategy) == 30
    assert summary["paired_workload_complete"] is True
    assert summary["observations_match_defined_models"] is True
    assert summary["guarded_model_safe"] is True
    assert summary["experiment_valid"] is True
    a = summary["strategies"][Strategy.NAIVE_RETRY.value]["counts"]
    b = summary["strategies"][Strategy.CHECKPOINT_ONLY.value]["counts"]
    c = summary["strategies"][Strategy.GUARDED.value]["counts"]
    assert a["duplicate_effects"] == 8
    assert b["unproven_next_dispatches"] == 3
    assert c["trials_with_safety_violations"] == 0
    assert all(sample.error is None for sample in samples)
