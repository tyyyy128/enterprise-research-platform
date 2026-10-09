"""Opt-in, small end-to-end 5C microbenchmark smoke against isolated UUID Runs."""

from __future__ import annotations

import asyncio
import os

import pytest

from enterprise_research.benchmarking.metrics import summarize
from enterprise_research.benchmarking.postgres_harness import SCENARIOS, BenchmarkConfig, run_postgres_benchmark

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK") != "1",
    reason="5C benchmark requires both opt-in flags and a migrated local enterprise_research development DB.",
)


def test_recovery_benchmark_real_database_smoke():
    samples, seconds = asyncio.run(
        run_postgres_benchmark(BenchmarkConfig(trials_per_scenario=1, concurrency=2, warmup_per_scenario=0))
    )
    assert len(samples) == len(SCENARIOS)
    assert {item.scenario for item in samples} == set(SCENARIOS)
    summary = summarize(samples, duration_s=seconds)
    assert summary["counts"]["eligible_dispatched"] == 1
    assert summary["counts"]["correctly_paused"] == 2
    assert summary["counts"]["successful_lock_trials"] == 1
    assert summary["counts"]["stale_write_attempts"] == 1
    assert summary["all_invariants_passed"] is True
