"""Pure offline 3I-2 repeated-run aggregation tests."""

from __future__ import annotations

import json

import pytest

from enterprise_research.acceptance.summary import _percentiles, aggregate_directory, summarize


def _record(status, elapsed):
    return {
        "harness": "3I-2",
        "status": status,
        "elapsed_seconds": elapsed,
        "outbound": {
            "requests_started": 3,
            "model_usage_observed": 1,
            "prompt_tokens_observed": 200,
            "completion_tokens_observed": 50,
            "tavily_credits_observed": 0.5,
        },
        "database": {
            "failure_category": "report_synthesis" if status != "completed" else None,
            "artifacts": {
                "question_search_coverage": 1.0,
                "mechanically_verified_citation_ratio": 1.0,
            },
            "stage_journal": [{"stage": "planning", "duration_seconds": elapsed / 2}],
        },
    }


def test_summary_withholds_quantiles_when_too_few_runs():
    one = summarize([_record("completed", 8)])
    assert one["elapsed"]["p50_seconds"] is None
    assert one["completion_rate"] == 1.0
    assert one["model_prompt_tokens_observed"] == 200
    assert one["mechanical_citation_lineage"]["observed_completed_runs"] == 1


def test_summary_computes_disclosed_nearest_rank_p95_and_no_quality_invention():
    runs = [_record("completed", elapsed) for elapsed in [1, 3, 5, 7, 9]]
    runs.append(_record("failed", 12))
    result = summarize(runs)
    assert result["run_count"] == 6
    assert result["completed_runs"] == 5
    assert result["completion_rate"] == 0.8333
    assert result["elapsed"]["p50_seconds"] == 6.0
    assert result["elapsed"]["p95_seconds"] == 12.0
    assert result["failure_categories"] == {"report_synthesis": 1}
    assert "semantic_accuracy" not in result
    assert result["mechanical_question_search_coverage"]["observed_completed_runs"] == 5


def test_empty_sample_and_nonmatching_files_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="At least one"):
        aggregate_directory(tmp_path)
    (tmp_path / "run-test.json").write_text(json.dumps({"harness": "random"}))
    with pytest.raises(ValueError, match="Non-3I-2"):
        aggregate_directory(tmp_path)


def test_aggregator_does_not_read_other_json_files(tmp_path):
    (tmp_path / "summary.json").write_text("not valid JSON")
    (tmp_path / "run-123.json").write_text(json.dumps(_record("failed", 4)))
    result = aggregate_directory(tmp_path)
    assert result["run_count"] == 1
    assert result["statuses"] == {"failed": 1}


def test_percentile_boundary():
    assert _percentiles([])["sample_count"] == 0
    assert _percentiles([1, 2, 3, 4, 5])["p95_seconds"] == 5
