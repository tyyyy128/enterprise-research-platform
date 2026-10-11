"""Offline aggregation for 3I-2 live acceptance JSON files.

This module never reads credentials, connects to providers or accesses PostgreSQL.
Sample percentiles are intentionally withheld for sample sizes below five.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


def _percentiles(samples: list[float]) -> dict[str, float | int | None]:
    values = sorted(samples)
    count = len(values)
    return {
        "sample_count": count,
        "p50_seconds": round(median(values), 3) if count >= 5 else None,
        "p95_seconds": round(values[math.ceil(count * 0.95) - 1], 3) if count >= 5 else None,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        raise ValueError("At least one live acceptance run JSON is required")
    status_counts = Counter(record.get("status", "unknown") for record in records)
    failure_counts = Counter(
        record.get("database", {}).get("failure_category") or record.get("harness_failure_category") or "unspecified"
        for record in records
        if record.get("status") != "completed"
    )
    seconds: list[float] = []
    by_stage: dict[str, list[float]] = defaultdict(list)
    for record in records:
        duration = record.get("elapsed_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            seconds.append(float(duration))
        for stage in record.get("database", {}).get("stage_journal", []):
            measured = stage.get("duration_seconds")
            if isinstance(measured, (int, float)) and measured >= 0:
                by_stage[str(stage["stage"])].append(float(measured))

    def observation(name: str) -> dict[str, int | float | None]:
        vals = [
            record.get("database", {}).get("artifacts", {}).get(name)
            for record in records
            if record.get("status") == "completed"
        ]
        numeric = [float(value) for value in vals if isinstance(value, (int, float)) and not isinstance(value, bool)]
        return {
            "observed_completed_runs": len(numeric),
            "mean": round(sum(numeric) / len(numeric), 4) if numeric else None,
        }

    outbound = [record.get("outbound", {}) for record in records]
    return {
        "method": "3I-2 real provider trial aggregation, no control group",
        "run_count": len(records),
        "completed_runs": status_counts["completed"],
        "completion_rate": round(status_counts["completed"] / len(records), 4),
        "statuses": dict(status_counts),
        "failure_categories": dict(failure_counts),
        "elapsed": _percentiles(seconds),
        "stage_latency": {key: _percentiles(value) for key, value in sorted(by_stage.items())},
        "external_requests_started": sum(int(item.get("requests_started", 0)) for item in outbound),
        "model_prompt_tokens_observed": sum(int(item.get("prompt_tokens_observed", 0)) for item in outbound),
        "model_completion_tokens_observed": sum(int(item.get("completion_tokens_observed", 0)) for item in outbound),
        "model_usage_observed_requests": sum(int(item.get("model_usage_observed", 0)) for item in outbound),
        "tavily_credits_observed": round(sum(float(item.get("tavily_credits_observed", 0)) for item in outbound), 4),
        "mechanical_question_search_coverage": observation("question_search_coverage"),
        "mechanical_citation_lineage": observation("mechanically_verified_citation_ratio"),
        "limitations": [
            "P50/P95 are not reported for fewer than five samples.",
            "Observed provider usage is incomplete if any response omitted usage.",
            "Neither citation-lineage nor source-coverage ratios prove semantic truth.",
            "Model quality requires an independently labeled human review set.",
            "No production speedup can be inferred without a comparable baseline and workload.",
        ],
    }


def aggregate_directory(directory: Path) -> dict[str, Any]:
    if not directory.is_dir():
        raise ValueError("Acceptance result directory does not exist")
    inputs = sorted([*directory.glob("run-*.json"), *directory.glob("pre-run-*.json")])
    records = []
    for path in inputs:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entry, dict) or entry.get("harness") != "3I-2":
            raise ValueError("Non-3I-2 data found in live acceptance file")
        records.append(entry)
    return summarize(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize existing 3I-2 results OFFLINE (no provider calls).")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write an additional aggregate JSON file")
    args = parser.parse_args(argv)
    try:
        result = aggregate_directory(args.input_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"3I-2 aggregation failed: {type(exc).__name__}\n")
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        if not args.output.is_absolute() or args.output.resolve().is_relative_to(Path.cwd().resolve()):
            parser.exit(2, "Output must be an absolute path outside the project checkout\n")
        from enterprise_research.acceptance.live_harness import _write_private

        _write_private(args.output, output + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
