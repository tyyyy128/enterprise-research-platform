"""5C-3: verified, separate aggregates for real resume and synthetic policy models.

No PostgreSQL or production executor access in this module. Source reports
remain immutable; each batch's raw samples are independently re-accounted.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from enterprise_research.benchmarking.metrics import (
    SCHEMA_VERSION as REAL_SCHEMA,
)
from enterprise_research.benchmarking.metrics import (
    RecoveryBenchmarkSample,
    latency_summary,
    nearest_rank,
    summarize,
)
from enterprise_research.benchmarking.strategy_comparison import (
    FAULT_WINDOWS,
    ComparisonSample,
    Strategy,
    summarize_comparison,
)
from enterprise_research.benchmarking.strategy_comparison import (
    SCHEMA_VERSION as MODEL_SCHEMA,
)

SUITE_SCHEMA = "3E-6D-5C.3"
REPORT_FILES = ("report.json", "samples.csv", "report.md")
RECOVERY_SCENARIOS = {
    "ready_two_workers": True,
    "started_with_plan": False,
    "completed_without_plan": False,
    "contended_initial_claim": None,
}
KINDS = ("recovery", "model")
MAX_REPORT_BYTES = 16 * 1024 * 1024
BOOTSTRAP_DRAWS = 600


@dataclass(frozen=True, slots=True)
class SuitePlan:
    """Bounded experimental design; a run is one independent CLI invocation."""

    seeds: tuple[int, ...] = (42, 43, 44)
    concurrencies: tuple[int, ...] = (1, 2)
    repetitions: int = 2
    trials: int = 10
    warmup: int = 1
    lock_hold_ms: int = 25

    def __post_init__(self) -> None:
        if (
            not isinstance(self.seeds, (list, tuple))
            or not 1 <= len(self.seeds) <= 5
            or len(set(self.seeds)) != len(self.seeds)
            or any(type(s) is not int or not 0 <= s <= 2**32 - 1 for s in self.seeds)
        ):
            raise ValueError("seeds must contain 1–5 unique uint32 integers.")
        if (
            not isinstance(self.concurrencies, (list, tuple))
            or not 1 <= len(self.concurrencies) <= 4
            or len(set(self.concurrencies)) != len(self.concurrencies)
            or any(type(c) is not int or not 1 <= c <= 4 for c in self.concurrencies)
        ):
            raise ValueError("concurrencies must be distinct integers in [1, 4].")
        for name, low, high in (
            ("repetitions", 1, 3),
            ("trials", 1, 100),
            ("warmup", 0, 5),
            ("lock_hold_ms", 10, 150),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}].")
        if len(self.seeds) * len(self.concurrencies) * self.repetitions * 2 > 40:
            raise ValueError("At most 40 child benchmark invocations are allowed per suite.")


@dataclass(frozen=True, slots=True)
class RunSpec:
    kind: str
    concurrency: int
    seed: int
    repetition: int

    @property
    def directory(self) -> str:
        return f"{self.kind}/c{self.concurrency}-s{self.seed}-r{self.repetition:02d}"


def planned_runs(plan: SuitePlan) -> list[RunSpec]:
    return [
        RunSpec(kind, concurrency, seed, repetition)
        for concurrency in plan.concurrencies
        for seed in plan.seeds
        for repetition in range(1, plan.repetitions + 1)
        for kind in KINDS
    ]


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def _read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Source report missing or exceeds 16 MiB safety limit.")

    def reject_constant(value: str) -> None:
        raise ValueError(f"Nonfinite JSON constant ({value}) is forbidden.")

    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError("Report root must be a JSON object.")
    return data


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_metadata(data: dict, spec: RunSpec, plan: SuitePlan) -> None:
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        raise ValueError("Source report is missing metadata.")
    required = {
        "seed": spec.seed,
        "concurrency": spec.concurrency,
        "trials_per_scenario" if spec.kind == "recovery" else "trials_per_window": plan.trials,
        "warmup_per_scenario" if spec.kind == "recovery" else "warmup_per_window": plan.warmup,
    }
    if spec.kind == "recovery":
        required["lock_hold_ms"] = plan.lock_hold_ms
    if any(type(meta.get(key)) is not type(value) or meta.get(key) != value for key, value in required.items()):
        raise ValueError(f"Source report metadata disagrees with suite plan ({spec.directory}).")
    head = meta.get("git_head")
    if not isinstance(head, str) or not head or head == "unknown" or not all(c in "0123456789abcdef" for c in head):
        raise ValueError("Benchmark reports require a concrete Git commit ID.")


def load_verified_report(report_dir: Path, spec: RunSpec, plan: SuitePlan) -> tuple[dict, list]:
    """Reject stale/inconsistent summary even if its JSON syntax is valid."""
    path = report_dir / "report.json"
    data = _read_json(path)
    _verify_metadata(data, spec, plan)
    rows = data.get("samples")
    if not isinstance(rows, list):
        raise ValueError("Report samples must be a list.")
    expected = (
        len(RECOVERY_SCENARIOS) * plan.trials
        if spec.kind == "recovery"
        else len(Strategy) * len(FAULT_WINDOWS) * plan.trials
    )
    if len(rows) != expected:
        raise ValueError(f"Incorrect sample count in {spec.directory}.")
    sample_type = RecoveryBenchmarkSample if spec.kind == "recovery" else ComparisonSample
    try:
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("Every sample must be an object.")
        samples = [sample_type(**row) for row in rows]
    except (TypeError, ValueError) as exc:
        raise ValueError("Malformed source sample data.") from exc
    summary = data.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Source summary missing.")
    if spec.kind == "recovery":
        if data.get("schema_version") != REAL_SCHEMA:
            raise ValueError("Unexpected real Safe Resume report version.")
        actual_keys = [(sample.scenario, sample.trial) for sample in samples]
        expected_keys = {(scenario, trial) for scenario in RECOVERY_SCENARIOS for trial in range(plan.trials)}
        if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != expected_keys:
            raise ValueError("Incomplete or duplicated Safe Resume scenario matrix.")
        if any(sample.expected_ready is not RECOVERY_SCENARIOS[sample.scenario] for sample in samples):
            raise ValueError("Scenario recovery disposition changed unexpectedly.")
        if any(
            (sample.expected_ready is None and sample.lock_claim_ms is None)
            or (sample.expected_ready is not None and (sample.inspect_ms is None or sample.decision_ms is None))
            for sample in samples
        ):
            raise ValueError("A required Safe Resume timing observation is missing.")
        measured = summarize(samples, duration_s=summary["batch"]["duration_s"])
        valid = measured["all_invariants_passed"]
    else:
        if data.get("schema_version") != MODEL_SCHEMA:
            raise ValueError("Unexpected synthetic model report version.")
        measured = summarize_comparison(
            samples, duration_s=summary["batch_duration_s_including_setup_cleanup"], trials_per_window=plan.trials
        )
        valid = measured["experiment_valid"]
    if summary != measured or not valid:
        raise ValueError(f"Source report failed independent safety and summary verification ({spec.directory}).")
    if any(not (report_dir / name).is_file() for name in REPORT_FILES):
        raise ValueError("One or more source report artifacts are missing.")
    return data, samples


def wilson_95(success: int, total: int) -> tuple[float, float] | None:
    """Exploratory Wilson interval; independence is NOT established by this benchmark."""
    if type(total) is not int or type(success) is not int or total < 0 or not 0 <= success <= total:
        raise ValueError("Invalid Wilson count or denominator.")
    if not total:
        return None
    z = 1.959963984540054
    p = success / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return (0.0 if success == 0 else max(0.0, center - half)), (1.0 if success == total else min(1.0, center + half))


def zero_event_upper_95(events: int, total: int) -> float | None:
    """Hypothetical iid Bernoulli one-sided 95% upper bound when zero observed."""
    if type(events) is not int or type(total) is not int or not 0 <= events <= total:
        raise ValueError("Invalid zero-event count or denominator.")
    if events or not total:
        return None
    return -math.expm1(math.log(0.05) / total)


def _latency(values_by_run: list[list[float]], *, seed_count: int, bootstrap_seed: int) -> dict:
    values = [value for run in values_by_run for value in run]
    result = latency_summary(values)
    result["cluster_bootstrap_p50_interval95_ms"] = None
    if len(values_by_run) < 5 or seed_count < 3 or not values:
        return result
    rng = random.Random(bootstrap_seed)
    p50s = []
    for _ in range(BOOTSTRAP_DRAWS):
        chosen = [values_by_run[rng.randrange(len(values_by_run))] for _ in values_by_run]
        pooled = [value for run in chosen for value in run]
        p50s.append(nearest_rank(pooled, 0.5))
    result["cluster_bootstrap_p50_interval95_ms"] = [
        nearest_rank(p50s, 0.025),
        nearest_rank(p50s, 0.975),
    ]
    return result


def _ratio(num: int, denom: int) -> float | None:
    return None if denom == 0 else num / denom


def _percentage(num: int, denom: int) -> str:
    value = _ratio(num, denom)
    return "N/A" if value is None else f"{value:.2%} ({num}/{denom})"


def _latency_text(record: dict) -> str:
    if not record["n"]:
        return "N/A"
    return f"{record['p50_ms']:.2f} / {record['p95_ms']:.2f} / {record['p99_ms']:.2f} (n={record['n']})"


def aggregate_verified_runs(records: list[tuple[RunSpec, dict, list]]) -> dict:
    """Never pool real Resume latencies with illustrative strategy-model latencies."""
    if not records:
        raise ValueError("An empty collection has no benchmark conclusion.")
    heads = {report["metadata"]["git_head"] for _, report, _ in records}
    if len(heads) != 1:
        raise ValueError("Suite contains different Git revisions; abort cross-run aggregation.")
    result: dict = {
        "schema_version": SUITE_SCHEMA,
        "git_head": next(iter(heads)),
        "run_count": len(records),
        "real_resume_by_concurrency": {},
        "synthetic_models_by_concurrency": {},
        "statistical_assumptions": {
            "quantile": "pooled nearest-rank; P99 is descriptive especially for small N",
            "bootstrap": (
                "batch-cluster resampling, exploratory P50 interval only when >=5 batches and >=3 distinct seeds"
            ),
            "binomial": (
                "Wilson/zero-event bounds are hypothetical iid Bernoulli "
                "sensitivity calculations, not proven production risks"
            ),
            "synthetic": (
                "fixed failure matrix and declared policies; no inferential confidence limits or production speedups"
            ),
            "throughput": (
                "within-workload batch trial/cell throughput, includes setup and cleanup, excludes CLI startup"
            ),
        },
    }
    for kind in KINDS:
        concurrencies = sorted({spec.concurrency for spec, _, _ in records if spec.kind == kind})
        for concurrency in concurrencies:
            subset = [
                (spec, report, samples)
                for spec, report, samples in records
                if spec.kind == kind and spec.concurrency == concurrency
            ]
            seed_count = len({spec.seed for spec, _, _ in subset})
            if kind == "recovery":
                results = [report["summary"] for _, report, _ in subset]
                counts = {key: sum(summary["counts"][key] for summary in results) for key in results[0]["counts"]}
                duration = sum(summary["batch"]["duration_s"] for summary in results)
                measured = {
                    "run_count": len(subset),
                    "distinct_seeds": seed_count,
                    "counts": counts,
                    "rates": {
                        "eligible_dispatch": _ratio(counts["eligible_dispatched"], counts["eligible_trials"]),
                        "correct_pause": _ratio(counts["correctly_paused"], counts["must_pause_trials"]),
                        "unauthorized_dispatches": _ratio(counts["unauthorized_dispatches"], counts["recovery_trials"]),
                        "stale_accepted": _ratio(counts["stale_write_accepted"], counts["stale_write_attempts"]),
                    },
                    "exploratory_iid_intervals": {
                        "eligible_dispatch_wilson95": wilson_95(
                            counts["eligible_dispatched"], counts["eligible_trials"]
                        ),
                        "correct_pause_wilson95": wilson_95(counts["correctly_paused"], counts["must_pause_trials"]),
                        "unauthorized_dispatch_zero_upper95": zero_event_upper_95(
                            counts["unauthorized_dispatches"], counts["recovery_trials"]
                        ),
                        "stale_accepted_zero_upper95": zero_event_upper_95(
                            counts["stale_write_accepted"], counts["stale_write_attempts"]
                        ),
                    },
                    "latency": {},
                    "batch_throughput_trials_per_s": counts["trials"] / duration,
                }
                for idx, (label, field) in enumerate(
                    (
                        ("eligible_dispatch", "decision_ms"),
                        ("pause_decision", "decision_ms"),
                        ("read_only_inspection", "inspect_ms"),
                        ("contended_claim_roundtrip", "lock_claim_ms"),
                    )
                ):

                    def eligible(sample: RecoveryBenchmarkSample) -> bool:
                        if label == "eligible_dispatch":
                            return sample.expected_ready is True and sample.dispatch_count == 1
                        if label == "pause_decision":
                            return sample.expected_ready is False and sample.dispatch_count == 0
                        if label == "read_only_inspection":
                            return sample.inspect_ms is not None
                        return sample.expected_ready is None

                    values = [
                        [
                            getattr(sample, field)
                            for sample in samples
                            if eligible(sample) and getattr(sample, field) is not None
                        ]
                        for _, _, samples in subset
                    ]
                    measured["latency"][label] = _latency(
                        values, seed_count=seed_count, bootstrap_seed=202610 + concurrency * 20 + idx
                    )
                result["real_resume_by_concurrency"][str(concurrency)] = measured
            else:
                duration = sum(report["summary"]["batch_duration_s_including_setup_cleanup"] for _, report, _ in subset)
                strategies = {}
                for index, strategy in enumerate(Strategy):
                    rows_by_run = [[s for s in samples if s.strategy == strategy.value] for _, _, samples in subset]
                    flat = [row for group in rows_by_run for row in group]
                    unsafe = sum(row.violated_safety for row in flat)
                    attempts = sum(row.stale_write_attempts for row in flat)
                    accepted = sum(row.stale_write_accepted for row in flat)
                    strategies[strategy.value] = {
                        "trials": len(flat),
                        "unsafe_trials": unsafe,
                        "duplicate_effects": sum(row.duplicated_effects for row in flat),
                        "unproven_advances": sum(row.unproven_next_dispatches for row in flat),
                        "missing_skips": sum(row.missing_artifact_skips for row in flat),
                        "stale_accepted": accepted,
                        "stale_attempts": attempts,
                        "unsafe_trials_fraction": unsafe / len(flat),
                        "latency": {
                            field: _latency(
                                [[getattr(row, field) for row in group] for group in rows_by_run],
                                seed_count=seed_count,
                                bootstrap_seed=202710 + concurrency * 50 + index * 2 + i,
                            )
                            for i, field in enumerate(("decision_ms", "operation_ms"))
                        },
                    }
                total_cells = sum(s["trials"] for s in strategies.values())
                result["synthetic_models_by_concurrency"][str(concurrency)] = {
                    "run_count": len(subset),
                    "distinct_seeds": seed_count,
                    "strategy_cells": total_cells,
                    "strategies": strategies,
                    "batch_throughput_policy_cells_per_s": total_cells / duration,
                }
    return result


def _bar_svg(title: str, ylabel: str, labels: list[str], values: list[float], descriptions: list[str]) -> str:
    """Standalone plain SVG, deliberately without browser JS or external assets."""
    if not labels or len(labels) != len(values) or len(labels) != len(descriptions):
        raise ValueError("SVG values and labels must match.")
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("SVG values must be finite and nonnegative.")
    width = max(620, 160 * len(labels) + 100)
    height = 340
    top, baseline, left = 73, 270, 72
    right = width - 35
    maximum = max(values) or 1
    step = (right - left) / len(labels)
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="27" font-family="sans-serif" font-size="18">{escape(title)}</text>',
        f'<text x="{left}" y="49" font-family="sans-serif" font-size="12">{escape(ylabel)}</text>',
        f'<line x1="{left}" y1="{baseline}" x2="{right}" y2="{baseline}" stroke="black"/>',
    ]
    for idx, (label, value, description) in enumerate(zip(labels, values, descriptions, strict=True)):
        x = left + (idx + 0.5) * step
        bar_h = (baseline - top) * value / maximum
        pieces.extend(
            [
                f'<rect x="{x - step * 0.22:.1f}" y="{baseline - bar_h:.1f}" '
                f'width="{step * 0.44:.1f}" height="{bar_h:.1f}" fill="gray"/>',
                f'<text x="{x:.1f}" y="{baseline - bar_h - 8:.1f}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="12">{value:.2f}</text>',
                f'<text x="{x:.1f}" y="292" text-anchor="middle" font-family="sans-serif" '
                f'font-size="12">{escape(label)}</text>',
                f'<text x="{x:.1f}" y="310" text-anchor="middle" font-family="sans-serif" '
                f'font-size="10">{escape(description)}</text>',
            ]
        )
    pieces.append("</svg>")
    return "\n".join(pieces) + "\n"


def _render_markdown(data: dict) -> str:
    lines = [
        "# 3E-6D-5C-3 — 恢复可靠性重复实验统计报告",
        "",
        f"- 来源 Git commit：`{data['git_head']}`；已核验批次数：**{data['run_count']}**。",
        "- 5C-1 真实 Safe Resume 和 5C-2 合成策略模型**分别统计，禁止跨负载计算性能加速比**。",
        "- 分位数采用样本合并后的 nearest-rank；P99 在小样本下不稳定。",
        (
            "- 采集时工作区状态：**"
            + str(data.get("collection_git_worktree_status", "unknown"))
            + "**；dirty/unknown 采样不能视作已提交源码的完整可复现证明。"
        ),
        "- P50 区间是按批次重采样的探索性 Bootstrap 区间，至少需要 5 批且 3 个不同种子。",
        "- Wilson / 零违规上界仅表示**假设独立同分布 Bernoulli**下的敏感性计算；",
        "  受控故障窗口高度相关，**不能据此宣称生产事故概率上界**。",
        "- 合成策略的风险由事先声明的故障矩阵决定，不应对其事故率附加统计置信区间。",
        "",
        "## A. 真实 Safe Resume（阶段交接，不是完整研究完成）",
        "",
        "| Parallel trials | Batches / Seeds | Eligible dispatch | Correct pause | Unauthorized | Stale accepted |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for concurrency, run in data["real_resume_by_concurrency"].items():
        cnt = run["counts"]
        lines.append(
            f"| {concurrency} | {run['run_count']} / {run['distinct_seeds']} | "
            f"{_percentage(cnt['eligible_dispatched'], cnt['eligible_trials'])} | "
            f"{_percentage(cnt['correctly_paused'], cnt['must_pause_trials'])} | "
            f"{_percentage(cnt['unauthorized_dispatches'], cnt['recovery_trials'])} | "
            f"{_percentage(cnt['stale_write_accepted'], cnt['stale_write_attempts'])} |"
        )
    lines += [
        "",
        "| Parallel trials | Measured batch workload throughput (trials/s) |",
        "|---:|---:|",
    ]
    for concurrency, run in data["real_resume_by_concurrency"].items():
        lines.append(f"| {concurrency} | {run['batch_throughput_trials_per_s']:.2f} |")
    lines += [
        "",
        "| Parallel trials | Metric | P50 / P95 / P99 (ms) | P50 cluster bootstrap 95% (ms) |",
        "|---:|---|---|---|",
    ]
    for concurrency, run in data["real_resume_by_concurrency"].items():
        for name, latency in run["latency"].items():
            interval = latency["cluster_bootstrap_p50_interval95_ms"]
            ci = "N/A (insufficient batches/seeds)" if interval is None else f"{interval[0]:.2f} – {interval[1]:.2f}"
            lines.append(f"| {concurrency} | {name} | {_latency_text(latency)} | {ci} |")
    lines += [
        "",
        "### 条件性二项分布区间（仅作 iid 敏感性分析，不能解释为生产可靠性界限）",
        "",
        "| Parallel trials | Eligible Wilson 95% | Correct pause Wilson 95% | "
        "Unauthorized zero-event upper 95% | Stale write zero-event upper 95% |",
        "|---:|---|---|---|---|",
    ]
    for concurrency, run in data["real_resume_by_concurrency"].items():
        bounds = run["exploratory_iid_intervals"]

        def format_interval(key: str) -> str:
            interval = bounds[key]
            return "N/A" if interval is None else f"[{interval[0]:.2%}, {interval[1]:.2%}]"

        def format_bound(key: str) -> str:
            bound = bounds[key]
            return "N/A (not zero / no denominator)" if bound is None else f"{bound:.2%}"

        lines.append(
            f"| {concurrency} | {format_interval('eligible_dispatch_wilson95')} | "
            f"{format_interval('correct_pause_wilson95')} | "
            f"{format_bound('unauthorized_dispatch_zero_upper95')} | "
            f"{format_bound('stale_accepted_zero_upper95')} |"
        )
    lines += [
        "",
        "![Safe Resume dispatch latency](recovery_dispatch_p50.svg)",
        "",
        "## B. 配对 PostgreSQL TEMP 表策略模型（不是生产 A/B）",
        "",
        "| Parallel trials | Strategy model | Unsafe trials | Duplicate effects | "
        "Unproven advances | Missing skips | Stale accepted |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for concurrency, run in data["synthetic_models_by_concurrency"].items():
        for name, stats in run["strategies"].items():
            lines.append(
                f"| {concurrency} | `{name}` | {stats['unsafe_trials']}/{stats['trials']} | "
                f"{stats['duplicate_effects']} | {stats['unproven_advances']} | "
                f"{stats['missing_skips']} | {stats['stale_accepted']}/{stats['stale_attempts']} |"
            )
    lines += [
        "",
        "| Parallel trials | Synthetic batch throughput (policy cells/s) |",
        "|---:|---:|",
    ]
    for concurrency, run in data["synthetic_models_by_concurrency"].items():
        lines.append(f"| {concurrency} | {run['batch_throughput_policy_cells_per_s']:.2f} |")
    lines += [
        "",
        "| Parallel trials | Strategy model | Decision P50 / P95 / P99 (ms) | Operation P50 / P95 / P99 (ms) |",
        "|---:|---|---|---|",
    ]
    for concurrency, run in data["synthetic_models_by_concurrency"].items():
        for name, stats in run["strategies"].items():
            lines.append(
                f"| {concurrency} | `{name}` | {_latency_text(stats['latency']['decision_ms'])} | "
                f"{_latency_text(stats['latency']['operation_ms'])} |"
            )
    lines += [
        "",
        "![Declared model hazards](model_unsafe_trials.svg)",
        "",
        "## 解释边界与可复现性",
        "",
        "- 此处并发度为每批 Benchmark 的参数，批次是**顺序**启动的，避免不同配置的实验互相污染。",
        "- 不报告跨负载绝对耗时差、不报告策略模型的生产性能排名。",
        "- P50 Bootstrap 区间是探索性统计描述；机器负载、固定故障窗口和同种子重复会导致样本非独立。",
        "- 真实恢复的零违规仅说明本实验未观察到违规。结合 5A SIGKILL 与 5B 竞争测试做工程证据。",
        "- 分析从每个 source `report.json` 原始 samples 重新计算指标，并对 manifest 记录的三份原始文件校验 SHA-256。",
        "- 详见同目录 `report.json`、`metrics.csv`、`runs.csv` 和原始各批次的报告。",
        "",
    ]
    return "\n".join(lines)


def write_suite_report(output_dir: Path, aggregate: dict, records: list[tuple[RunSpec, dict, list]]) -> None:
    """All output is new; preserve independently generated source reports."""
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "report.json").write_text(_canonical_json(aggregate), encoding="utf-8")
    (output_dir / "report.md").write_text(_render_markdown(aggregate), encoding="utf-8")
    with (output_dir / "runs.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["kind", "concurrency", "seed", "repetition", "samples", "batch_duration_s", "git_head"])
        for spec, report, samples in records:
            summary = report["summary"]
            elapsed = (
                summary["batch"]["duration_s"]
                if spec.kind == "recovery"
                else summary["batch_duration_s_including_setup_cleanup"]
            )
            writer.writerow(
                [spec.kind, spec.concurrency, spec.seed, spec.repetition, len(samples), elapsed, aggregate["git_head"]]
            )
    with (output_dir / "metrics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "kind",
                "concurrency",
                "strategy",
                "metric",
                "n",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "bootstrap_p50_low",
                "bootstrap_p50_high",
            ]
        )

        def write_latency(kind: str, concurrency: str, strategy: str, metric: str, data: dict) -> None:
            ci = data["cluster_bootstrap_p50_interval95_ms"] or (None, None)
            writer.writerow(
                [kind, concurrency, strategy, metric, data["n"], data["p50_ms"], data["p95_ms"], data["p99_ms"], *ci]
            )

        for concurrency, group in aggregate["real_resume_by_concurrency"].items():
            for name, latency in group["latency"].items():
                write_latency("recovery", concurrency, "production_resume", name, latency)
        for concurrency, group in aggregate["synthetic_models_by_concurrency"].items():
            for strategy, stats in group["strategies"].items():
                for name, latency in stats["latency"].items():
                    write_latency("model", concurrency, strategy, name, latency)
    real = aggregate["real_resume_by_concurrency"]
    (output_dir / "recovery_dispatch_p50.svg").write_text(
        _bar_svg(
            "Real Safe Resume — Dispatch P50",
            "Milliseconds (client-observed)",
            [f"c={c}" for c in real],
            [run["latency"]["eligible_dispatch"]["p50_ms"] for run in real.values()],
            [f"n={run['latency']['eligible_dispatch']['n']}" for run in real.values()],
        ),
        encoding="utf-8",
    )
    model = aggregate["synthetic_models_by_concurrency"]
    (output_dir / "model_unsafe_trials.svg").write_text(
        _bar_svg(
            "Modeled Unsafe Trials — Fixed Fault Matrix",
            "Percent of synthetic trials (NOT production probability)",
            ["Naive retry", "Checkpoint-only", "Guarded"],
            [
                100
                * sum(m["strategies"][name]["unsafe_trials"] for m in model.values())
                / sum(m["strategies"][name]["trials"] for m in model.values())
                for name in Strategy
            ],
            ["synthetic"] * len(Strategy),
        ),
        encoding="utf-8",
    )
