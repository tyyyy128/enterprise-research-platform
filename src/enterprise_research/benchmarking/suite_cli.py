"""5C-3: repeat both *separate* existing benchmarks, verify and analyze reports.

Collect mode requires all three explicit development-DB opt-ins. Analyze mode
never creates a database connection; it audits a complete immutable manifest.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from enterprise_research.benchmarking.statistical_suite import (
    REPORT_FILES,
    SUITE_SCHEMA,
    RunSpec,
    SuitePlan,
    _canonical_json,
    _read_json,
    aggregate_verified_runs,
    load_verified_report,
    planned_runs,
    sha256_file,
    write_suite_report,
)

Runner = Callable[[RunSpec, Path, SuitePlan], None]


def _git_worktree_status() -> str:
    """Source reproducibility label; never assume HEAD means unmodified code."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=False, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode:
        return "unknown"
    return "dirty" if result.stdout else "clean"


REQUIRED_FLAGS = (
    "ENTERPRISE_RESEARCH_RUN_DB_TESTS",
    "ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK",
    "ENTERPRISE_RESEARCH_STRATEGY_COMPARISON",
)


def _write_manifest(root: Path, manifest: dict) -> None:
    staged = root / "manifest.json.tmp"
    staged.write_text(_canonical_json(manifest), encoding="utf-8")
    staged.replace(root / "manifest.json")


def _invoke_existing_cli(spec: RunSpec, destination: Path, plan: SuitePlan) -> None:
    module = (
        "enterprise_research.benchmarking.cli"
        if spec.kind == "recovery"
        else "enterprise_research.benchmarking.compare_cli"
    )
    command = [
        sys.executable,
        "-m",
        module,
        "--trials",
        str(plan.trials),
        "--warmup",
        str(plan.warmup),
        "--concurrency",
        str(spec.concurrency),
        "--seed",
        str(spec.seed),
        "--output-dir",
        str(destination),
    ]
    if spec.kind == "recovery":
        command.extend(["--lock-hold-ms", str(plan.lock_hold_ms)])
    try:
        finished = subprocess.run(command, capture_output=True, text=True, check=False, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Child benchmark exceeded its 600-second safety timeout.") from exc
    if finished.returncode != 0:
        # Do not print stdout/stderr: DB exception text can contain credentials.
        raise RuntimeError(f"Child benchmark returned nonzero status ({finished.returncode}).")


def collect_suite(root: Path, plan: SuitePlan, *, runner: Runner | None = None) -> dict:
    """Always start with a new folder; leave partial manifest on failure."""
    if any(os.getenv(name) != "1" for name in REQUIRED_FLAGS):
        raise PermissionError("Collect requires all three explicit PostgreSQL development-benchmark flags.")
    specs = planned_runs(plan)
    root.mkdir(parents=True, exist_ok=False)
    manifest: dict = {
        "schema_version": SUITE_SCHEMA,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "git_worktree_at_collection": _git_worktree_status(),
        "plan": {
            "seeds": list(plan.seeds),
            "concurrencies": list(plan.concurrencies),
            "repetitions": plan.repetitions,
            "trials": plan.trials,
            "warmup": plan.warmup,
            "lock_hold_ms": plan.lock_hold_ms,
        },
        "expected_jobs": len(specs),
        "jobs": [],
        "complete": False,
    }
    _write_manifest(root, manifest)
    run = runner or _invoke_existing_cli
    for index, spec in enumerate(specs, 1):
        print(f"[{index}/{len(specs)}] {spec.directory}", flush=True)
        path = root / spec.directory
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            run(spec, path, plan)
            load_verified_report(path, spec, plan)
        except Exception:
            # No claim of completion, no rewriting or deleting the raw outputs.
            manifest["failed_job"] = spec.directory
            _write_manifest(root, manifest)
            raise
        manifest["jobs"].append(
            {
                "kind": spec.kind,
                "seed": spec.seed,
                "concurrency": spec.concurrency,
                "repetition": spec.repetition,
                "directory": spec.directory,
                "sha256": {filename: sha256_file(path / filename) for filename in REPORT_FILES},
            }
        )
        _write_manifest(root, manifest)
    manifest["complete"] = True
    _write_manifest(root, manifest)
    aggregate, records = analyze_verified_suite(root)
    write_suite_report(root / "analysis", aggregate, records)
    return aggregate


def analyze_verified_suite(root: Path) -> tuple[dict, list]:
    """Manifest paths are reconstructed, never trusted as arbitrary filesystem paths."""
    manifest = _read_json(root / "manifest.json")
    if manifest.get("schema_version") != SUITE_SCHEMA or manifest.get("complete") is not True:
        raise ValueError("Incomplete suite: partial results are not a valid aggregate.")
    fields = manifest.get("plan")
    if not isinstance(fields, dict):
        raise ValueError("Suite manifest has no experiment plan.")
    try:
        plan = SuitePlan(**fields)
    except (TypeError, ValueError) as exc:
        raise ValueError("Malformed experiment plan.") from exc
    specs = planned_runs(plan)
    entries = manifest.get("jobs")
    if not isinstance(entries, list) or len(entries) != len(specs) or manifest.get("expected_jobs") != len(specs):
        raise ValueError("Suite manifest does not contain the complete planned run matrix.")
    records = []
    for spec, entry in zip(specs, entries, strict=True):
        expected = {
            "kind": spec.kind,
            "seed": spec.seed,
            "concurrency": spec.concurrency,
            "repetition": spec.repetition,
            "directory": spec.directory,
        }
        if not isinstance(entry, dict) or any(entry.get(key) != val for key, val in expected.items()):
            raise ValueError("Manifest run identity/order conflicts with experiment plan.")
        report_dir = root / spec.directory
        # Refuse any symlink inside a report directory (including the directory itself).
        if (
            (root / spec.kind).is_symlink()
            or report_dir.is_symlink()
            or any((report_dir / name).is_symlink() for name in REPORT_FILES)
        ):
            raise ValueError("Symlink source report paths are forbidden.")
        hashes = entry.get("sha256")
        if not isinstance(hashes, dict) or set(hashes) != set(REPORT_FILES):
            raise ValueError("Manifest report checksums missing.")
        for filename in REPORT_FILES:
            report_file = report_dir / filename
            if not report_file.is_file() or sha256_file(report_file) != hashes[filename]:
                raise ValueError(f"Source artifact checksum mismatch ({spec.directory}/{filename}).")
        data, samples = load_verified_report(report_dir, spec, plan)
        records.append((spec, data, samples))
    aggregate = aggregate_verified_runs(records)
    git_worktree = manifest.get("git_worktree_at_collection", "unknown")
    if git_worktree not in ("clean", "dirty", "unknown"):
        raise ValueError("Invalid collection Git worktree status.")
    aggregate["collection_git_worktree_status"] = git_worktree
    return aggregate, records


def _parse_int_tuple(raw: str, *, name: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part.strip()) for part in raw.split(","))
    except ValueError as exc:
        raise ValueError(f"{name} must be comma-separated integers.") from exc
    if not values or any(not part.strip() for part in raw.split(",")):
        raise ValueError(f"{name} must not be empty.")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5C-3: repeat and audit real/model benchmarks without mixing results")
    sub = parser.add_subparsers(dest="mode", required=True)
    collect = sub.add_parser("collect", help="Opt-in local PostgreSQL multi-run collection plus analysis")
    collect.add_argument("--output-dir", type=Path, required=True)
    collect.add_argument("--seeds", default="42,43,44", help="Comma-separated distinct uint32 seeds")
    collect.add_argument("--concurrencies", default="1,2", help="Comma-separated benchmark concurrency settings")
    collect.add_argument("--repeats", type=int, default=2, help="Complete batches per seed/concurrency, 1–3")
    collect.add_argument("--trials", type=int, default=10, help="Per scenario/window measured repetitions, 1–100")
    collect.add_argument("--warmup", type=int, default=1, help="Warmup repetitions per scenario/window, 0–5")
    collect.add_argument("--lock-hold-ms", type=int, default=25, help="Real benchmark forced lock hold, 10–150 ms")
    analyze = sub.add_parser("analyze", help="Offline analysis of a completed 5C-3 manifest")
    analyze.add_argument("--suite-dir", type=Path, required=True)
    analyze.add_argument("--output-dir", type=Path, required=True, help="A NEW output folder, never overwrite")
    args = parser.parse_args(argv)
    try:
        if args.mode == "collect":
            plan = SuitePlan(
                seeds=_parse_int_tuple(args.seeds, name="seeds"),
                concurrencies=_parse_int_tuple(args.concurrencies, name="concurrencies"),
                repetitions=args.repeats,
                trials=args.trials,
                warmup=args.warmup,
                lock_hold_ms=args.lock_hold_ms,
            )
            aggregate = collect_suite(args.output_dir, plan)
            out = args.output_dir / "analysis"
        else:
            if args.output_dir.exists():
                raise FileExistsError("Analysis output already exists; preserve previous reports.")
            aggregate, records = analyze_verified_suite(args.suite_dir)
            write_suite_report(args.output_dir, aggregate, records)
            out = args.output_dir
    except (ValueError, FileExistsError, PermissionError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Do not print exception messages that may contain database credentials.
        print(f"STOP: suite execution failed ({type(exc).__name__}); no valid conclusion.", file=sys.stderr)
        return 1
    print(f"Verified batches: {aggregate['run_count']}")
    print("Real Safe Resume and synthetic strategy-model results remain separate.")
    print(f"Analysis: {out}/report.md, report.json, metrics.csv, runs.csv, two SVG charts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
