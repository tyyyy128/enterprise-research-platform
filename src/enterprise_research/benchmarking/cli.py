"""Run 5C development-only recovery benchmarks and write immutable reports.

Usage (from project root, after safety preflight):
    ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1 \\
      uv run --no-project python -m enterprise_research.benchmarking.cli \\
      --trials 10 --concurrency 2 --output-dir reports/recovery-5c-run01
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from enterprise_research.benchmarking.metrics import write_report
from enterprise_research.benchmarking.postgres_harness import BenchmarkConfig, run_postgres_benchmark


def _commit_label() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5C: bounded PostgreSQL Safe Resume microbenchmark")
    parser.add_argument("--trials", type=int, default=5, help="Measured trials per scenario (1–100)")
    parser.add_argument("--warmup", type=int, default=1, help="Unmeasured warmup trials per scenario (0–10)")
    parser.add_argument("--concurrency", type=int, default=2, help="Max parallel trials (1–4)")
    parser.add_argument("--lock-hold-ms", type=int, default=25, help="Forced row-lock hold in milliseconds (10–150)")
    parser.add_argument("--seed", type=int, default=42, help="Workload-order seed (does not fix OS scheduling)")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New directory for report.json, samples.csv, report.md"
    )
    args = parser.parse_args(argv)
    try:
        config = BenchmarkConfig(
            trials_per_scenario=args.trials,
            concurrency=args.concurrency,
            warmup_per_scenario=args.warmup,
            lock_hold_ms=args.lock_hold_ms,
            seed=args.seed,
        )
        if args.output_dir.exists():
            raise FileExistsError("Output directory already exists; choose a new path to avoid overwriting results.")
        samples, elapsed_s = asyncio.run(run_postgres_benchmark(config))
        metadata = {
            "measured_at_utc": datetime.now(UTC).isoformat(),
            "git_head": _commit_label(),
            "python": platform.python_version(),
            "platform": platform.system(),
            "database": "local enterprise_research (isolated UUID Task/Run)",
            "checkpoint_reader": "not_configured",
            "lease_expiry": "test-only scoped SQL injection (not a real process kill)",
            "recovery_outcome": "next-stage dispatch observed; full research task not completed",
            "strategy": "journal_and_plan_and_lease_fencing",
            "strategy_comparison": "not_implemented",
            "trials_per_scenario": config.trials_per_scenario,
            "warmup_per_scenario": config.warmup_per_scenario,
            "concurrency": config.concurrency,
            "lock_hold_ms": config.lock_hold_ms,
            "seed": config.seed,
        }
        summary = write_report(args.output_dir, samples=samples, duration_s=elapsed_s, metadata=metadata)
    except (ValueError, PermissionError, FileExistsError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Database exception text may include password or internal host detail.
        print(f"STOP: benchmark execution failed ({type(exc).__name__}). No success claim.", file=sys.stderr)
        return 1
    print(f"Measured trials: {summary['counts']['trials']}")
    print(f"Invariant verification: {'PASS' if summary['all_invariants_passed'] else 'FAIL'}")
    print(f"Reports: {args.output_dir}/report.json, samples.csv, report.md")
    return 0 if summary["all_invariants_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
