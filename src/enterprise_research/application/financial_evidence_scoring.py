"""Bounded lexical ranking for exact annual total-revenue table evidence.

This is a *selection hint*, not a financial parser or a factual verdict.
Only intact, previously persisted source chunks are ever returned to the model.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from enterprise_research.application.evidence_extraction import DocumentChunk

# Require a full (not substring) year. A paragraph mentioning FY2024 is not
# sufficient by itself: the year must appear near an apparent financial row.
_YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_TOTAL_REVENUE_ROW = re.compile(r"(?im)^[ \t|]*total[ \t]+revenues?\b")
_GROUPED_AMOUNT = re.compile(r"(?<![\w.])(?:[$€£]\s*)?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w.])")
_UNIT_PATTERN = re.compile(
    r"\b(?:in\s+(?:usd\s+)?millions?|usd\s+millions?|million\s+(?:us\s+)?dollars?"
    r"|in\s+billions?)\b|百万美元|亿元",
    re.IGNORECASE,
)
_PERIOD_HEADING = re.compile(r"\b(?:years?\s+ended|fiscal\s+years?)\b", re.IGNORECASE)

# Only explicit report-edition cues are authoritative here. Opaque CDN paths
# do not prove any year; query strings and fragments are intentionally ignored.
_REPORT_EDITION_PATH = re.compile(
    r"(?:^|[/_.-])(?:ar|fy)(?P<year>(?:19|20)\d{2}|\d{2})(?=$|[/_.-])",
    re.IGNORECASE,
)
_REPORT_EDITION_COVER = re.compile(
    r"\b(?:(?P<before>(?:19|20)\d{2})\s+(?:annual\s+report|form\s+10-?k)"
    r"|(?:annual\s+report|form\s+10-?k)\s*[:\-]?\s*(?P<after>(?:19|20)\d{2})"
    r"|(?:fy|fiscal(?:\s+year)?)\s*(?P<fiscal>(?:19|20)\d{2})\s+annual\s+report)\b",
    re.IGNORECASE,
)
_REPORT_EDITION_FISCAL_END = re.compile(
    r"\b(?:for\s+the\s+)?fiscal\s+year\s+ended\s+"
    r"(?:june\s+30,?\s*|\w+\s+\d{1,2},?\s+)(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def _primary_annual_columns(years: list[str], *, target_year: str) -> bool:
    """Require the requested year first, with strictly descending columns."""
    return (
        len(years) >= 2
        and years[0] == target_year
        and all(int(first) > int(second) for first, second in zip(years, years[1:], strict=False))
    )


def declared_report_edition_years(source_url: str | None, cover_text: str) -> frozenset[str]:
    """Extract explicit year-edition cues without inferring an unknown CDN version."""
    years: set[str] = set()
    if source_url:
        try:
            parsed = urlsplit(source_url)
            if parsed.scheme.casefold() in {"http", "https"}:
                for match in _REPORT_EDITION_PATH.finditer(parsed.path):
                    raw = match.group("year")
                    years.add("20" + raw if len(raw) == 2 else raw)
        except ValueError:
            pass
    for match in _REPORT_EDITION_COVER.finditer(cover_text[:2500]):
        years.add(next(value for value in match.groupdict().values() if value))
    for match in _REPORT_EDITION_FISCAL_END.finditer(cover_text[:2500]):
        years.add(match.group("year"))
    return frozenset(years)


_STATEMENT_PATTERN = re.compile(
    r"\b(?:income statements?|(?:consolidated\s+)?statements?\s+of\s+(?:income|operations)"
    r"|summary\s+results\s+of\s+operations)\b|利润表|损益表",
    re.IGNORECASE,
)

# HTML-to-text providers may flatten an entire income statement into one
# physical line. Recognize that format only when the *same original chunk*
# contains a statement title, a year-column header, units and an amount row.
# Never infer the fiscal year from an unrelated narrative mention.
_FLAT_INCOME_STATEMENT = re.compile(r"\bincome\s+statements?\b", re.IGNORECASE)
_FLAT_YEAR_COLUMNS = re.compile(
    r"\byear\s+ended\s+june\s+30,?\s*\|?\s*"
    r"(?P<years>(?:19|20)\d{2}(?:\s*\|?\s*(?:19|20)\d{2}){1,3})\b",
    re.IGNORECASE,
)
_FLAT_AMOUNT = r"(?:\$\s*)?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
_FLAT_REVENUE_ROW = re.compile(
    rf"\btotal[ \t]+revenues?\b[ \t|]+"
    rf"(?P<amounts>{_FLAT_AMOUNT}(?:[ \t|]+{_FLAT_AMOUNT}){{1,3}})(?![\d,])",
    re.IGNORECASE,
)


def _flattened_income_statement_tier(text: str, *, year: str) -> int:
    """Conservative lexical hint for a fully inline annual income statement.

    A row of figures without its *own* header and units never qualifies.
    This does not determine factual correctness or rewrite any text/offsets.
    """
    for row in _FLAT_REVENUE_ROW.finditer(text):
        before = text[max(0, row.start() - 700) : row.start()]
        statements = list(_FLAT_INCOME_STATEMENT.finditer(before))
        if not statements:
            continue
        statement_context = before[statements[-1].start() :]
        headers = list(_FLAT_YEAR_COLUMNS.finditer(statement_context))
        if not headers:
            continue
        header = headers[-1]  # Most recent local fiscal-year header.
        years = _YEAR_PATTERN.findall(header.group("years"))
        # The lead column identifies this income statement's report year.
        # A later 2026 report can contain an accurate FY2024 comparison
        # amount, but it must not pass as the original FY2024 report.
        if not _primary_annual_columns(years, target_year=year) or len(years) != len(
            _GROUPED_AMOUNT.findall(row.group("amounts"))
        ):
            continue
        if _UNIT_PATTERN.search(statement_context[: header.start()]) is None:
            continue
        return 4
    return 0


def target_financial_year(focus_terms: tuple[str, ...]) -> str | None:
    """Extract one unambiguous target year from an explicit focus vocabulary."""
    years = {match.group() for term in focus_terms for match in _YEAR_PATTERN.finditer(term)}
    return next(iter(years)) if len(years) == 1 else None


def is_preferred_source(source_url: str | None, preferred_domains: tuple[str, ...]) -> bool:
    """Compare HTTPS hostnames by DNS label, never by substring or URL text."""
    if not source_url or not preferred_domains:
        return False
    try:
        parsed = urlsplit(source_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and any(
        host == domain.strip().casefold() or host.endswith("." + domain.strip().casefold())
        for domain in preferred_domains
    )


def financial_precision_rank(chunk: DocumentChunk, *, year: str, focus_terms: tuple[str, ...]) -> tuple[int, int]:
    """Return (table-evidence tier, distinct focus hits) for an original chunk.

    Tiers favor an annual Total revenue row with a comma-grouped amount,
    target fiscal year as the lead year of the nearest table header, units
    and statement heading. This is a table-vintage filter, not independent
    proof of the source's publication year or the amount's correctness.
    """
    text = chunk.content
    folded = text.casefold()
    hits = sum(term.casefold().strip() in folded for term in focus_terms)
    if not hits:
        return (0, 0)

    best_tier = 0
    for row in _TOTAL_REVENUE_ROW.finditer(text):
        # Include a short continuation for Markdown/html-to-text row wrapping,
        # but never attach surrounding paragraphs or create a synthetic chunk.
        row_excerpt = text[row.start() : min(len(text), row.end() + 260)]
        if _GROUPED_AMOUNT.search(row_excerpt) is None:
            continue
        context = text[max(0, row.start() - 900) : min(len(text), row.end() + 260)]
        if re.search(rf"(?<!\d){re.escape(year)}(?!\d)", context) is None:
            continue
        # Require an explicit, nearby period header with the requested year
        # as its FIRST column. The FY2026/FY2025/FY2024 comparison table
        # cannot substitute for the original FY2024 income statement.
        preceding_lines = text[max(0, row.start() - 900) : row.start()].splitlines()
        period_headers = [
            line
            for line in preceding_lines
            if len(line) <= 200 and (_PERIOD_HEADING.search(line) or len(_YEAR_PATTERN.findall(line)) >= 2)
        ]
        if not period_headers:
            continue
        years = _YEAR_PATTERN.findall(period_headers[-1])
        if not _primary_annual_columns(years, target_year=year):
            continue
        has_units = _UNIT_PATTERN.search(context) is not None
        has_heading = _STATEMENT_PATTERN.search(context) is not None
        best_tier = max(best_tier, 2 + int(has_units) + int(has_units and has_heading))

    # Also handle HTML-to-text output that destroys table line breaks.
    # Both layouts enforce descending columns beginning with the target year.
    best_tier = max(best_tier, _flattened_income_statement_tier(text, year=year))
    if best_tier:
        return (best_tier, hits)

    # Non-tabular revenue comments are still eligible as fallback, but cannot
    # outrank a genuine table candidate. Never hallucinate a missing amount.
    if re.search(rf"(?<!\d){re.escape(year)}(?!\d)", text) and "revenue" in folded:
        return (1, hits)
    return (0, hits)
