"""Live harness environment-isolation tests. No external requests or database."""

from __future__ import annotations

import pytest

from enterprise_research.acceptance.financial_workload import financial_workload_overrides
from enterprise_research.acceptance.live_harness import SMALL_WORKLOAD, temporary_workload_env


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


def test_financial_mode_overrides_only_bounded_knobs_and_restores_caller_env() -> None:
    env = _precision_env()
    original = env.copy()
    new_overrides = financial_workload_overrides(env)
    with temporary_workload_env(env):
        assert env["ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS"] == "450000"
        assert env["ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS"] == "2500"
        assert env["ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS"] == "600"
        for name, value in SMALL_WORKLOAD.items():
            if name not in new_overrides:
                assert env[name] == value
    assert env == original


def test_legacy_focus_mode_still_uses_12k_bounded_harness_and_restores_env() -> None:
    env = _precision_env()
    env["ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE"] = "focus"
    original = env.copy()
    with temporary_workload_env(env):
        assert env["ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS"] == "12000"
        assert env["ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS"] == "200"
    assert env == original


def test_exception_inside_harness_restores_all_original_values() -> None:
    env = _precision_env()
    original = env.copy()
    with pytest.raises(RuntimeError, match="test interruption"):
        with temporary_workload_env(env):
            raise RuntimeError("test interruption")
    assert env == original


def test_bad_financial_config_rejected_without_touching_environment() -> None:
    env = _precision_env()
    env["ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS"] = "9999999"
    original = env.copy()
    with pytest.raises(ValueError, match="MAX_CONTENT_CHARS"):
        with temporary_workload_env(env):
            raise AssertionError("should not run")
    assert env == original
