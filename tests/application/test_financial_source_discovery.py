"""3J-2 offline search-discovery checks: no provider or database access."""

from __future__ import annotations

import pytest

from enterprise_research.application.financial_source_discovery import (
    FinancialSourceDiscoverySettings,
    financial_discovery_query,
    rank_financial_discovery_hits,
)
from enterprise_research.application.search import SearchResult


@pytest.fixture
def policy() -> FinancialSourceDiscoverySettings:
    return FinancialSourceDiscoverySettings(target_year="2024", preferred_domains=("microsoft.com",))


def _hit(title: str, url: str) -> SearchResult:
    return SearchResult(title=title, url=url, snippet="Untrusted search snippet", provider="fixture")


def test_search_query_preserves_question_and_explicit_report_year(policy: FinancialSourceDiscoverySettings) -> None:
    query = financial_discovery_query("Microsoft total revenue?", policy)
    assert query.startswith("FY2024 original annual report official investor relations Form 10-K")
    assert "income statements total revenue 2024 2023 2022" in query
    assert query.endswith("Microsoft total revenue?")
    assert len(query) <= 400


def test_query_is_bounded_even_for_long_planned_questions(policy: FinancialSourceDiscoverySettings) -> None:
    query = financial_discovery_query("Microsoft revenue " + "x" * 10000, policy)
    assert len(query) == 400
    assert query.startswith("FY2024 original annual report")


@pytest.mark.parametrize("bad_year", ["24", "2024FY", "2024 2026", "", "3024"])
def test_bad_target_year_is_rejected(bad_year: str) -> None:
    with pytest.raises(ValueError, match="four-digit"):
        FinancialSourceDiscoverySettings(target_year=bad_year)


@pytest.mark.parametrize(
    "domains",
    [
        ("https://microsoft.com",),
        ("microsoft.com.evil.example/",),
        ("microsoft.com", "MICROSOFT.COM"),
        tuple(f"test{i}.example" for i in range(9)),
    ],
)
def test_invalid_domain_settings_fail_closed(domains: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        FinancialSourceDiscoverySettings(target_year="2024", preferred_domains=domains)


def test_preferred_original_report_beats_wrong_year_and_spoof_domain(
    policy: FinancialSourceDiscoverySettings,
) -> None:
    results = [
        _hit("Microsoft 2026 Annual Report", "https://www.microsoft.com/investor/reports/ar26/"),
        _hit("Financial results", "https://cdn.example/uuid-document"),
        _hit("Microsoft 2024 Annual Report", "https://www.microsoft.com.evil.example/investor/reports/ar24/"),
        _hit("Microsoft 2024 Annual Report", "https://www.microsoft.com/investor/reports/ar24/index.html"),
    ]
    ranked = rank_financial_discovery_hits(results, policy)
    assert [str(item.result.url) for item in ranked] == [
        "https://www.microsoft.com/investor/reports/ar24/index.html",
        "https://www.microsoft.com.evil.example/investor/reports/ar24/",
        "https://cdn.example/uuid-document",
    ]
    assert [(item.edition_hint, item.preferred_domain) for item in ranked] == [
        ("matching", True),
        ("matching", False),
        ("unknown", False),
    ]


@pytest.mark.parametrize(
    ("title", "url"),
    [
        ("Financial statement", "https://microsoft.com/investor/reports/ar26/index.html"),
        ("Microsoft 2026 Annual Report", "https://cdn.example/opaque-id"),
        ("Microsoft FY2026 annual report", "https://cdn.example/opaque-id"),
    ],
)
def test_wrong_edition_in_url_or_title_alone_is_rejected(
    title: str, url: str, policy: FinancialSourceDiscoverySettings
) -> None:
    assert rank_financial_discovery_hits([_hit(title, url)], policy) == []


def test_conflicting_url_and_title_report_editions_are_rejected(policy: FinancialSourceDiscoverySettings) -> None:
    results = [_hit("Microsoft 2024 Annual Report", "https://microsoft.com/investor/reports/ar26/")]
    assert rank_financial_discovery_hits(results, policy) == []


def test_comparison_year_in_snippet_does_not_certify_report_edition(
    policy: FinancialSourceDiscoverySettings,
) -> None:
    candidate = SearchResult(
        title="Microsoft annual financial table",
        url="https://cdn.example/opaque-id",
        snippet="FY2024 revenue appears in FY2026 historical comparison columns",
        provider="fixture",
    )
    ranked = rank_financial_discovery_hits([candidate], policy)
    assert len(ranked) == 1
    assert ranked[0].edition_hint == "unknown"


def test_stable_provider_order_for_equal_candidate_priority(policy: FinancialSourceDiscoverySettings) -> None:
    results = [_hit("Unknown B", "https://b.example/x"), _hit("Unknown A", "https://a.example/x")]
    ranked = rank_financial_discovery_hits(results, policy)
    assert [hit.result.title for hit in ranked] == ["Unknown B", "Unknown A"]
