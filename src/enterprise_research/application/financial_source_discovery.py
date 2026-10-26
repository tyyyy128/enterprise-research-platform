"""Bounded annual-report discovery hints; never proof of financial evidence."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from enterprise_research.application.financial_evidence_scoring import (
    declared_report_edition_years,
    is_preferred_source,
)
from enterprise_research.application.search import SearchResult

_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_MAX_QUERY_CHARS = 400


@dataclass(frozen=True, slots=True)
class FinancialSourceDiscoverySettings:
    """Explicit opt-in from the existing financial_precision workload mode."""

    target_year: str
    preferred_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.target_year, str) or not _YEAR.fullmatch(self.target_year):
            raise ValueError("Financial discovery requires one four-digit target year.")
        if len(self.preferred_domains) > 8:
            raise ValueError("Financial discovery accepts at most eight preferred domains.")
        seen: set[str] = set()
        for domain in self.preferred_domains:
            if not isinstance(domain, str) or len(domain) > 253 or not _DOMAIN.fullmatch(domain.lower()):
                raise ValueError("Financial discovery domains must be bare DNS hostnames.")
            key = domain.casefold()
            if key in seen:
                raise ValueError("Financial discovery preferred domains must be unique.")
            seen.add(key)


@dataclass(frozen=True, slots=True)
class FinancialDiscoveryHit:
    """One *unverified* search candidate and its lexical provenance hints."""

    result: SearchResult
    edition_hint: Literal["matching", "unknown"]
    preferred_domain: bool


def financial_discovery_query(question: str, settings: FinancialSourceDiscoverySettings) -> str:
    """Use one existing provider call, with a short report-vintage query prefix."""
    question = " ".join(question.split())
    if not question:
        raise ValueError("A planned research question must not be blank.")
    year = int(settings.target_year)
    prefix = (
        f"FY{year} original annual report official investor relations Form 10-K "
        f"income statements total revenue {year} {year - 1} {year - 2}"
    )
    available = _MAX_QUERY_CHARS - len(prefix) - 1
    return f"{prefix} {question[:available]}"


def rank_financial_discovery_hits(
    results: Sequence[SearchResult], settings: FinancialSourceDiscoverySettings
) -> list[FinancialDiscoveryHit]:
    """Reject explicit wrong editions, prioritize official hints, retain unknowns.

    URL paths and titles are untrusted *hints*. They cannot certify the retrieved
    document's edition, annual statement columns, amount, or provenance.
    """
    candidates: list[FinancialDiscoveryHit] = []
    for result in results:
        edition_years = declared_report_edition_years(str(result.url), result.title)
        # A URL/title identifying 2026 (or conflicting 2024 and 2026)
        # cannot be treated as a candidate for an original FY2024 report.
        if edition_years and edition_years != {settings.target_year}:
            continue
        candidates.append(
            FinancialDiscoveryHit(
                result=result,
                edition_hint="matching" if edition_years else "unknown",
                preferred_domain=is_preferred_source(str(result.url), settings.preferred_domains),
            )
        )

    # Stable: keep the provider's order for equal-priority results.
    return sorted(
        candidates,
        key=lambda hit: (-int(hit.preferred_domain), -int(hit.edition_hint == "matching")),
    )
