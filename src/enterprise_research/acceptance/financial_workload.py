"""Explicit, bounded financial-precision overrides for the live acceptance harness.

No provider or database calls are made by this module. Defaults for the original
3I-2 workload remain owned by ``live_harness.SMALL_WORKLOAD``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_MODE_KEY = "ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE"
_FOCUS_KEY = "ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS"
_MAX_CONTENT_KEY = "ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS"
_CHUNK_SIZE_KEY = "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS"
_OVERLAP_KEY = "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS"
_POLICY_KEY = "ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY"
_PREFERRED_KEY = "ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS"
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _financial_requested(env: Mapping[str, str]) -> bool:
    value = env.get(_MODE_KEY, "")
    return isinstance(value, str) and value.strip().lower() == "financial_precision"


def _bounded_decimal(
    env: Mapping[str, str],
    key: str,
    *,
    low: int,
    high: int,
    default: str | None = None,
) -> tuple[int | None, str | None]:
    raw = env.get(key, default)
    if not isinstance(raw, str) or len(raw) > 10 or not raw.isascii() or not raw.isdecimal():
        return None, f"{key} must be a decimal integer between {low} and {high}."
    value = int(raw)
    if not low <= value <= high:
        return None, f"{key} must be a decimal integer between {low} and {high}."
    return value, None


def financial_workload_issues(env: Mapping[str, str]) -> list[str]:
    """Return safe, bounded preflight errors; do not echo env values or secrets."""
    if not _financial_requested(env):
        return []

    problems: list[str] = []
    _, max_content_problem = _bounded_decimal(env, _MAX_CONTENT_KEY, low=12001, high=450000)
    if max_content_problem:
        problems.append(max_content_problem)

    chunk_size, chunk_problem = _bounded_decimal(env, _CHUNK_SIZE_KEY, low=256, high=2500, default="2500")
    if chunk_problem:
        problems.append(chunk_problem)

    overlap, overlap_problem = _bounded_decimal(env, _OVERLAP_KEY, low=0, high=600, default="200")
    if overlap_problem:
        problems.append(overlap_problem)
    elif chunk_size is not None and overlap is not None and overlap >= chunk_size:
        problems.append(f"{_OVERLAP_KEY} must be smaller than {_CHUNK_SIZE_KEY}.")

    focus = env.get(_FOCUS_KEY, "")
    terms = tuple(term.strip().lower() for term in focus.split(",") if term.strip()) if isinstance(focus, str) else ()
    years = {match.group() for term in terms for match in _YEAR_RE.finditer(term)}
    if (
        not 1 <= len(terms) <= 8
        or len(years) != 1
        or not any("revenue" in term for term in terms)
        or any(len(term) > 64 or "\n" in term or "\r" in term for term in terms)
    ):
        problems.append(f"{_FOCUS_KEY} must contain one target fiscal year and revenue, with 1-8 terms.")

    policy = env.get(_POLICY_KEY, "reject")
    if not isinstance(policy, str) or policy.strip().lower() not in {"reject", "truncate"}:
        problems.append(f"{_POLICY_KEY} must be 'reject' or 'truncate'.")
    return problems


def financial_workload_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """Override only the allowed content/chunk knobs in explicit precision mode.

    The original model/Tavily request budgets and one-chunk workload are unchanged.
    """
    problems = financial_workload_issues(env)
    if problems:
        raise ValueError("; ".join(problems))
    if not _financial_requested(env):
        return {}
    overrides = {
        _MODE_KEY: "financial_precision",
        _FOCUS_KEY: env[_FOCUS_KEY],
        _MAX_CONTENT_KEY: str(int(env[_MAX_CONTENT_KEY])),
        _CHUNK_SIZE_KEY: str(int(env.get(_CHUNK_SIZE_KEY, "2500"))),
        _OVERLAP_KEY: str(int(env.get(_OVERLAP_KEY, "200"))),
        _POLICY_KEY: env.get(_POLICY_KEY, "reject").strip().lower(),
    }
    if _PREFERRED_KEY in env:
        overrides[_PREFERRED_KEY] = env[_PREFERRED_KEY]
    return overrides
