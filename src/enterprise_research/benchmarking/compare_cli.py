"""Run the isolated 5C-2 paired strategy *model* comparison.

    ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1 \\
      uv run --no-project python -m enterprise_research.benchmarking.compare_cli \\
      --trials 10 --warmup 1 --concurrency 2 --seed 42 \\
      --output-dir "$HOME/benchmarks/enterprise-research/5c-2-run01"
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from enterprise_research.benchmarking.postgres_strategy_comparison import ComparisonConfig, run_postgres_comparison
from enterprise_research.benchmarking.strategy_comparison import FAULT_WINDOWS, write_comparison_report


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False, timeout=3
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5C-2: paired PostgreSQL TEMP-table strategy model comparison")
    parser.add_argument("--trials", type=int, default=5, help="Measured repetitions per fault window, 1–100")
    parser.add_argument("--warmup", type=int, default=1, help="Unmeasured warmup repetitions per window, 0–5")
    parser.add_argument("--concurrency", type=int, default=2, help="Parallel isolated paired blocks, 1–4")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic window and strategy-order shuffle")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory (reports cannot be overwritten)")
    args = parser.parse_args(argv)
    try:
        config = ComparisonConfig(
            trials_per_window=args.trials,
            warmup_per_window=args.warmup,
            concurrency=args.concurrency,
            seed=args.seed,
        )
        if args.output_dir.exists():
            raise FileExistsError("Output directory exists; use a new directory, never overwrite benchmark history.")
        samples, duration_s = asyncio.run(run_postgres_comparison(config))
        metadata = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "git_head": _git_head(),
            "python": platform.python_version(),
            "platform": platform.system(),
            "database": "local enterprise_research; session-local TEMP tables only",
            "scope": "synthetic PLANNING artifact replay and stale-write comparison; no production executor invoked",
            "paired_fault_windows": [window.name for window in FAULT_WINDOWS],
            "trials_per_window": config.trials_per_window,
            "warmup_per_window": config.warmup_per_window,
            "concurrency": config.concurrency,
            "seed": config.seed,
            "checkpoint": "synthetic recorded snapshot, not live LangGraph saver",
            "model_contract": "naive retry, checkpoint-only, simplified journal+artifact+generation fence",
        }
        summary = write_comparison_report(
            args.output_dir,
            samples=samples,
            duration_s=duration_s,
            trials_per_window=config.trials_per_window,
            metadata=metadata,
        )
    except (ValueError, PermissionError, FileExistsError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"STOP: strategy comparison failed ({type(exc).__name__}); no valid benchmark claim.", file=sys.stderr)
        return 1
    print(f"Measured policy cells: {summary['samples']}")
    print(f"Paired experiment validity: {'PASS' if summary['experiment_valid'] else 'FAIL'}")
    print("Baseline model hazards are measured outcomes, not test failures by themselves.")
    print(f"Reports: {args.output_dir}/report.json, samples.csv, report.md")
    return 0 if summary["experiment_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
