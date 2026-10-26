"""Offline, zero-provider tests of the bounded precision acceptance opt-in."""

from __future__ import annotations

import pytest

from enterprise_research.acceptance.financial_workload import (
    financial_workload_issues,
    financial_workload_overrides,
)


def _precision_env() -> dict[str, str]:
    return {
        "ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE": "financial_precision",
        "ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS": "2024,revenue",
        "ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS": "microsoft.com",
        "ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS": "450000",
        "ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY": "truncate",
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS": "2500",
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS": "600",
    }


def test_default_modes_do_not_change_3i2_harness_overrides() -> None:
    for mode in (None, "uuid", "focus"):
        env = {"ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS": "450000"}
        if mode:
            env["ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE"] = mode
        assert financial_workload_issues(env) == []
        assert financial_workload_overrides(env) == {}


def test_explicit_financial_mode_accepts_bounded_large_document_without_changing_budgets() -> None:
    env = _precision_env()
    assert financial_workload_issues(env) == []
    assert financial_workload_overrides(env) == {
        "ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE": "financial_precision",
        "ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS": "2024,revenue",
        "ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS": "microsoft.com",
        "ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS": "450000",
        "ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY": "truncate",
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS": "2500",
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS": "600",
    }


def test_default_financial_chunk_limits_remain_small() -> None:
    env = _precision_env()
    env.pop("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS")
    env.pop("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS")
    override = financial_workload_overrides(env)
    assert override["ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS"] == "2500"
    assert override["ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS"] == "200"


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", ""),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "12000"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "450001"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "1000000"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "-1"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "450000.0"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS", "４５００００"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS", "2501"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS", "0"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS", "601"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS", "2500"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS", "2024"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS", "2023,2024,revenue"),
        ("ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS", "revenue"),
        ("ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY", "ignore"),
    ],
)
def test_invalid_financial_configuration_refused_before_provider_calls(key: str, invalid: str) -> None:
    env = _precision_env()
    env[key] = invalid
    assert financial_workload_issues(env)
    with pytest.raises(ValueError):
        financial_workload_overrides(env)


def test_missing_explicit_large_document_limit_is_rejected() -> None:
    env = _precision_env()
    env.pop("ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS")
    assert financial_workload_issues(env)
    with pytest.raises(ValueError):
        financial_workload_overrides(env)


def test_no_secret_content_appears_in_errors() -> None:
    env = _precision_env()
    env["ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS"] = "private-api-key-secret"
    assert "private-api-key-secret" not in "; ".join(financial_workload_issues(env))
