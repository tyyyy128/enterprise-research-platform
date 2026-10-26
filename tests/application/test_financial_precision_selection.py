"""Offline, exact-value 3J-1 evidence ranking regression fixtures."""

from __future__ import annotations

from uuid import UUID

import pytest

from enterprise_research.application.document_chunking import DocumentChunkingSettings, chunk_document
from enterprise_research.application.evidence_extraction import DocumentChunk
from enterprise_research.application.evidence_workload import (
    EvidenceDocumentChunks,
    EvidenceWorkloadSettings,
    plan_evidence_workload,
)
from enterprise_research.application.financial_evidence_scoring import (
    financial_precision_rank,
    is_preferred_source,
)


def _document(source_num: int, text: str, *, url: str | None = None, chunk_chars: int = 1000) -> EvidenceDocumentChunks:
    return EvidenceDocumentChunks(
        source_id=UUID(int=source_num),
        chunks=tuple(chunk_document(text, DocumentChunkingSettings(chunk_size_chars=chunk_chars, overlap_chars=0))),
        source_url=url,
    )


def _precision(*, preferred_domains: tuple[str, ...] = ()) -> EvidenceWorkloadSettings:
    return EvidenceWorkloadSettings(
        selection_mode="financial_precision",
        focus_terms=("2024", "revenue"),
        preferred_domains=preferred_domains,
        max_documents=1,
        max_chunks_per_document=1,
        max_total_chunks=1,
    )


# Synthetic transcription of the *shape* of Microsoft's 2024 income statement;
# not a provider download. The correctness oracle is distinct from the scorer.
_FINANCIAL_TABLE = (
    "INCOME STATEMENTS\n"
    "(In millions, except per share amounts)\n"
    "Year Ended June 30 | 2024 | 2023 | 2022\n"
    "Revenue:\n"
    "Product | 64,773 | 64,699 | 72,732\n"
    "Service and other | 180,349 | 147,216 | 125,538\n"
    "Total revenue | 245,122 | 211,915 | 198,270\n"
)


def test_precise_annual_table_beats_management_summary_in_one_chunk_budget() -> None:
    overview = "2024 annual revenue: over $245 billion, up 16 percent year-over-year."
    # The table is intentionally beyond the old 12K prefix used by 3I-2.
    full_text = overview.ljust(13_000, ".") + "\n" + _FINANCIAL_TABLE
    docs = (
        _document(1, overview, url="https://www.microsoft.com/investor/reports/ar24/index.html"),
        _document(2, full_text, url="https://www.microsoft.com/investor/reports/ar24/index.html"),
    )
    result = plan_evidence_workload(docs, _precision())
    assert result.scheduled_chunk_count == 1
    assert result.selected_document_count == 1
    assert result.available_chunk_count > 10
    assert result.items[0].source_id == UUID(int=2)
    assert result.items[0].chunk.index > 12
    assert "Total revenue | 245,122" in result.items[0].chunk.content
    assert (
        result.items[0].chunk.content
        == full_text[result.items[0].chunk.start_offset : result.items[0].chunk.end_offset]
    )


def test_year_unit_and_statement_context_outweigh_loose_mentions() -> None:
    commentary = _document(1, "2024 revenue about $245 billion. Total cost of revenue 74,114")
    table = _document(2, _FINANCIAL_TABLE)
    result = plan_evidence_workload([commentary, table], _precision())
    assert result.items[0].source_id == UUID(int=2)
    assert financial_precision_rank(result.items[0].chunk, year="2024", focus_terms=("2024", "revenue")) == (
        4,
        2,
    )


def test_wrong_fiscal_year_table_does_not_outrank_matching_year_overview() -> None:
    wrong = _document(
        1,
        "INCOME STATEMENTS\n(In millions)\nYear Ended June 30 | 2023 | 2022\nTotal revenue | 211,915 | 198,270",
    )
    matching = _document(2, "In fiscal year 2024, annual revenue exceeded $245 billion.")
    result = plan_evidence_workload([wrong, matching], _precision())
    assert result.scheduled_chunk_count == 0
    assert result.items == ()


def test_2024_mentioned_elsewhere_cannot_override_2023_table_header() -> None:
    misleading = _document(
        1,
        "FY2024 corporate overview\n"
        + "More details follow.\n" * 5
        + "INCOME STATEMENTS\n(In millions)\nYear Ended June 30 | 2023 | 2022\n"
        + "Total revenue | 211,915 | 198,270",
    )
    result = plan_evidence_workload([misleading], _precision())
    assert result.scheduled_chunk_count == 0


def test_preferred_domain_breaks_quality_tie_but_spoof_domain_does_not() -> None:
    fake = _document(1, _FINANCIAL_TABLE, url="https://www.microsoft.com.evil.example/report")
    official = _document(2, _FINANCIAL_TABLE, url="https://www.microsoft.com/investor/reports/ar24/")
    settings = _precision(preferred_domains=("microsoft.com",))
    result = plan_evidence_workload([fake, official], settings)
    assert result.items[0].source_id == UUID(int=2)
    assert is_preferred_source(fake.source_url, ("microsoft.com",)) is False
    assert is_preferred_source("https://evil-microsoft.com/revenue", ("microsoft.com",)) is False
    assert not is_preferred_source("https://www.microsoft.com.evil.example/?redirect=microsoft.com", ("microsoft.com",))
    assert is_preferred_source("http://microsoft.com/", ("microsoft.com",)) is False
    assert is_preferred_source("https://WWW.MICROSOFT.COM/report", ("MICROSOFT.COM",)) is True


def test_preferred_official_host_overrides_minor_formatting_score_difference() -> None:
    official = _document(
        2,
        "(In millions)\n2024 | 2023\nTotal revenue | 245,122 | 211,915",
        url="https://microsoft.com/report",
    )
    more_decorated_third_party = _document(
        1,
        _FINANCIAL_TABLE,
        url="https://third-party.example/summary",
    )
    result = plan_evidence_workload(
        [more_decorated_third_party, official],
        _precision(preferred_domains=("microsoft.com",)),
    )
    assert result.items[0].source_id == UUID(int=2)


def test_wrong_metric_2024_cost_of_revenue_does_not_beat_total_revenue() -> None:
    cost = _document(1, "(In millions) 2024 2023\nTotal cost of revenue | 74,114 | 65,863")
    total = _document(2, _FINANCIAL_TABLE)
    result = plan_evidence_workload([cost, total], _precision())
    assert result.items[0].source_id == UUID(int=2)


def test_missing_qualified_table_fails_closed_without_model_work() -> None:
    docs = (_document(2, "2024 revenue greater than $245 billion"), _document(1, "2024: overview"))
    result = plan_evidence_workload(docs, _precision())
    assert result.selected_document_count == 0
    assert result.scheduled_chunk_count == 0
    assert result.skipped_chunk_count == result.available_chunk_count


def test_no_unit_or_no_year_never_satisfies_exact_table_gate() -> None:
    source_without_unit = _document(1, "Year Ended 2024 2023\nTotal revenue | 245,122 | 211,915")
    source_without_year = _document(2, "(In millions)\nTotal revenue | 245,122 | 211,915")
    result = plan_evidence_workload([source_without_unit, source_without_year], _precision())
    assert result.scheduled_chunk_count == 0


def test_split_table_header_and_amount_row_do_not_form_invented_evidence() -> None:
    header = "(In millions) 2024 23"
    row = "Total revenue | 245,122 | 211"
    docs = (
        EvidenceDocumentChunks(
            source_id=UUID(int=1),
            chunks=(
                DocumentChunk(index=0, start_offset=0, end_offset=len(header), content=header),
                DocumentChunk(index=1, start_offset=len(header), end_offset=len(header + row), content=row),
            ),
        ),
    )
    result = plan_evidence_workload(docs, _precision())
    assert result.scheduled_chunk_count == 0


def test_non_source_text_never_replaces_original_chunk_offsets() -> None:
    text = "intro".ljust(1000, ".") + "\n" + _FINANCIAL_TABLE
    result = plan_evidence_workload((_document(1, text, chunk_chars=1000),), _precision())
    chunk = result.items[0].chunk
    assert chunk.index >= 1
    assert text[chunk.start_offset : chunk.end_offset] == chunk.content
    assert "245,122" in chunk.content


@pytest.mark.parametrize(
    "kwargs",
    [
        {"selection_mode": "financial_precision", "focus_terms": ("revenue",)},
        {"selection_mode": "financial_precision", "focus_terms": ("2024",)},
        {"selection_mode": "financial_precision", "focus_terms": ("2023", "2024", "revenue")},
        {"selection_mode": "focus", "focus_terms": ("2024",), "preferred_domains": ("microsoft.com",)},
        {
            "selection_mode": "financial_precision",
            "focus_terms": ("2024", "revenue"),
            "preferred_domains": ("https://microsoft.com",),
        },
        {
            "selection_mode": "financial_precision",
            "focus_terms": ("2024", "revenue"),
            "preferred_domains": ("example.com", "EXAMPLE.COM"),
        },
        {
            "selection_mode": "financial_precision",
            "focus_terms": ("2024", "revenue"),
            "preferred_domains": tuple(f"a{i}.example" for i in range(9)),
        },
    ],
)
def test_financial_precision_rejects_ambiguous_or_unsafe_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EvidenceWorkloadSettings(**kwargs)


def test_year_matching_uses_boundaries_not_a_substring() -> None:
    chunk = DocumentChunk(
        index=0,
        start_offset=0,
        end_offset=len("Year 12024, revenue 245,122"),
        content="Year 12024, revenue 245,122",
    )
    assert financial_precision_rank(chunk, year="2024", focus_terms=("2024", "revenue")) == (0, 2)


# Faithful one-line transcription of the statement excerpt observed when
# Microsoft FY2024 HTML is passed through a whitespace-collapsing parser.
# A fixture alone is not a substitute for a live Tavily extract.
_INLINE_INCOME_STATEMENT = (
    "FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA "
    "INCOME STATEMENTS (In millions, except per share amounts) "
    "Year Ended June 30, 2024 2023 2022 "
    "Revenue: Product $ 64,773 $ 64,699 $ 72,732 "
    "Service and other 180,349 147,216 125,538 "
    "Total revenue 245,122 211,915 198,270 "
    "Cost of revenue: Product 15,272 17,804 19,064"
)


def test_flattened_income_statement_selected_from_144_chunks_with_original_offsets() -> None:
    overview = "In 2024 we delivered over $245 billion in annual revenue. "
    source_text = overview.ljust(152_000, ".") + _INLINE_INCOME_STATEMENT
    source_text = source_text.ljust(273_468, ".")
    chunks = tuple(chunk_document(source_text, DocumentChunkingSettings(chunk_size_chars=2500, overlap_chars=600)))
    assert len(chunks) == 144
    selected = plan_evidence_workload(
        (EvidenceDocumentChunks(source_id=UUID(int=1), chunks=chunks),),
        _precision(),
    )
    assert selected.scheduled_chunk_count == 1
    chunk = selected.items[0].chunk
    assert chunk.start_offset > 12000
    assert "Total revenue 245,122 211,915 198,270" in chunk.content
    assert "Year Ended June 30, 2024 2023 2022" in chunk.content
    assert "In millions" in chunk.content
    assert chunk.content == source_text[chunk.start_offset : chunk.end_offset]
    assert financial_precision_rank(chunk, year="2024", focus_terms=("2024", "revenue")) == (4, 2)


@pytest.mark.parametrize(
    "flat_text",
    [
        # A year elsewhere never substitutes for the explicit table columns.
        "FY2024 introduction "
        + _INLINE_INCOME_STATEMENT.replace("2024 2023 2022", "2023 2022").replace(
            "245,122 211,915 198,270", "211,915 198,270"
        ),
        # Reject an incorrect amount-column count even with the correct year.
        _INLINE_INCOME_STATEMENT.replace("245,122 211,915 198,270", "245,122 211,915"),
        # Unit must belong to the same statement, not earlier commentary.
        _INLINE_INCOME_STATEMENT.replace("(In millions, except per share amounts)", ""),
        _INLINE_INCOME_STATEMENT.replace("INCOME STATEMENTS", "MANAGEMENT OVERVIEW"),
        _INLINE_INCOME_STATEMENT.replace("Year Ended June 30, 2024 2023 2022", "FY2024"),
        _INLINE_INCOME_STATEMENT.replace("Total revenue 245,122", "Total cost of revenue 245,122"),
        _INLINE_INCOME_STATEMENT.replace("245,122 211,915 198,270", "245122 211915 198270"),
        # The year of the *nearest* header controls eligibility.
        _INLINE_INCOME_STATEMENT.replace("Revenue: Product", "Year Ended June 30, 2023 2022 Revenue: Product").replace(
            "245,122 211,915 198,270", "211,915 198,270"
        ),
        # Table title or header far outside the bounded context cannot qualify.
        _INLINE_INCOME_STATEMENT.replace("Revenue: Product", "irrelevant " * 80 + "Revenue: Product"),
    ],
)
def test_flattened_table_refuses_incomplete_or_misattributed_evidence(flat_text: str) -> None:
    result = plan_evidence_workload((_document(1, flat_text, chunk_chars=4000),), _precision())
    assert result.scheduled_chunk_count == 0
    assert result.items == ()


def test_flattened_table_not_combined_across_disjoint_chunks() -> None:
    header = "INCOME STATEMENTS (In millions) Year Ended June 30, 2024 2023 2022 "
    row = "Total revenue 245,122 211,915 198,270"
    pieces = (
        DocumentChunk(index=0, start_offset=0, end_offset=len(header), content=header),
        DocumentChunk(index=1, start_offset=len(header), end_offset=len(header + row), content=row),
    )
    result = plan_evidence_workload((EvidenceDocumentChunks(source_id=UUID(int=1), chunks=pieces),), _precision())
    assert result.scheduled_chunk_count == 0


# Redacted literal table excerpt observed in the 3J-1 live PostgreSQL source
# document. The FY2024 value is real, but FY2026 is the document's lead year.
_LIVE_FY2026_TABLE = (
    "# ITEM 8. FINANCIAL STATE MENTS AND SUPPLEMENTARY DATA\n"
    "INC OME STATEMENTS\n"
    "> (In millions, except per share amounts)\n"
    "> Year Ended June 30, 2026 2025 2024\n"
    "Revenue:\n"
    "Product $ 64,696 $ 63,946 $ 64,773\n"
    "Service and other 267,143 217,778 180,349\n"
    "Total revenue 331,839 281,724 245,122\n"
)


def test_live_fy2026_comparison_column_never_qualifies_as_fy2024_original_report() -> None:
    source = _document(1, _LIVE_FY2026_TABLE, url="https://microsoft.gcs-web.com/static-files/opaque")
    result = plan_evidence_workload((source,), _precision())
    assert result.scheduled_chunk_count == 0
    assert result.items == ()
    assert financial_precision_rank(source.chunks[0], year="2024", focus_terms=("2024", "revenue"))[0] < 3


def test_flat_fy2026_comparison_column_never_qualifies_as_fy2024_original_report() -> None:
    late_years = _INLINE_INCOME_STATEMENT.replace("2024 2023 2022", "2026 2025 2024").replace(
        "245,122 211,915 198,270", "331,839 281,724 245,122"
    )
    source = _document(1, late_years, chunk_chars=2500)
    assert financial_precision_rank(source.chunks[0], year="2024", focus_terms=("2024", "revenue"))[0] < 3
    result = plan_evidence_workload((source,), _precision())
    assert result.scheduled_chunk_count == 0


def test_fy2024_original_annual_report_wins_even_when_newer_comparison_report_first() -> None:
    newer = _document(1, _LIVE_FY2026_TABLE, url="https://microsoft.gcs-web.com/static-files/opaque")
    original = _document(
        2,
        _FINANCIAL_TABLE,
        url="https://www.microsoft.com/investor/reports/ar24/index.html",
    )
    result = plan_evidence_workload((newer, original), _precision())
    assert result.scheduled_chunk_count == 1
    assert result.items[0].source_id == UUID(int=2)
    assert "245,122 | 211,915 | 198,270" in result.items[0].chunk.content


def test_financial_precision_never_schedules_lower_tier_chunks_from_selected_source() -> None:
    first = _FINANCIAL_TABLE
    second = "INCOME STATEMENTS\n(In millions)\nYear Ended June 30, 2026 2025 2024\n"
    second += "Total revenue 331,839 281,724 245,122"
    source = _document(1, first.ljust(1000) + second.ljust(1000) + "About 2024 revenue.".ljust(1000), chunk_chars=1000)
    settings = EvidenceWorkloadSettings(
        selection_mode="financial_precision",
        focus_terms=("2024", "revenue"),
        max_documents=1,
        max_chunks_per_document=3,
        max_total_chunks=3,
    )
    result = plan_evidence_workload((source,), settings)
    assert result.scheduled_chunk_count == 1
    assert result.items[0].chunk.index == 0


def test_explicit_period_header_required_not_an_unrelated_year_in_overview() -> None:
    ambiguous = _document(
        1,
        "FY2024 overview of cloud spending.\nINCOME STATEMENTS\n(In millions)\nTotal revenue 245,122 211,915 198,270",
    )
    assert plan_evidence_workload((ambiguous,), _precision()).scheduled_chunk_count == 0


def test_multiline_tavily_income_statement_with_fy2024_lead_column_remains_eligible() -> None:
    # Tavily-style OCR/Markdown may insert spaces into the heading; the
    # anchored Total revenue row, year columns and units are still intact.
    original = _LIVE_FY2026_TABLE.replace("2026 2025 2024", "2024 2023 2022").replace(
        "331,839 281,724 245,122", "245,122 211,915 198,270"
    )
    source = _document(1, original, url="https://www.microsoft.com/investor/reports/ar24/index.html")
    assert financial_precision_rank(source.chunks[0], year="2024", focus_terms=("2024", "revenue"))[0] == 3
    result = plan_evidence_workload((source,), _precision())
    assert result.scheduled_chunk_count == 1
    assert result.items[0].chunk == source.chunks[0]


# 3J-1 Source-Year Fidelity: preserve the current leading-column and
# per-chunk gates, extending only the missing chronology and edition checks.


@pytest.mark.parametrize(
    "years",
    ["2024 2026 2025", "2024 2023 2025", "2024 2024 2023", "2026 2025 2024"],
)
def test_flat_income_statement_rejects_non_descending_or_wrong_lead_year(years: str) -> None:
    text = _INLINE_INCOME_STATEMENT.replace("2024 2023 2022", years)
    source = _document(1, text, chunk_chars=2500)
    assert plan_evidence_workload((source,), _precision()).scheduled_chunk_count == 0


@pytest.mark.parametrize(
    "years",
    ["2024 | 2026 | 2025", "2024 | 2024 | 2023", "2025 | 2024 | 2023"],
)
def test_multiline_income_statement_rejects_non_descending_years(years: str) -> None:
    text = _FINANCIAL_TABLE.replace("2024 | 2023 | 2022", years)
    source = _document(1, text)
    assert plan_evidence_workload((source,), _precision()).scheduled_chunk_count == 0


@pytest.mark.parametrize(
    "url,cover",
    [
        ("https://www.microsoft.com/investor/reports/ar26/index.html", ""),
        ("https://www.microsoft.com/investor/reports/ar2026/index.html?ref=/ar24/", ""),
        ("https://www.microsoft.com/investor/reports/fy2026/", ""),
        ("https://microsoft.gcs-web.com/static-files/opaque", "2026 Annual Report\n"),
        ("https://microsoft.gcs-web.com/static-files/opaque", "ANNUAL REPORT 2026\n"),
        ("https://microsoft.gcs-web.com/static-files/opaque", "FY2026 Annual Report\n"),
        ("https://microsoft.gcs-web.com/static-files/opaque", "Form 10-K 2026\n"),
        ("https://microsoft.gcs-web.com/static-files/opaque", "For the fiscal year ended June 30, 2026\n"),
        ("https://www.microsoft.com/investor/reports/ar24/", "2026 Annual Report\n"),
    ],
)
def test_report_edition_mismatch_rejects_even_apparently_valid_fy2024_table(url: str, cover: str) -> None:
    source = _document(1, cover + _FINANCIAL_TABLE, url=url, chunk_chars=2500)
    assert plan_evidence_workload((source,), _precision()).scheduled_chunk_count == 0


def test_fy2024_original_and_opaque_cdn_remain_eligible() -> None:
    original = _document(
        1,
        "Microsoft 2024 Annual Report\n" + _FINANCIAL_TABLE,
        url="https://www.microsoft.com/investor/reports/ar24/",
    )
    unknown = _document(2, _FINANCIAL_TABLE, url="https://microsoft.gcs-web.com/static-files/opaque")
    assert plan_evidence_workload((original,), _precision()).scheduled_chunk_count == 1
    assert plan_evidence_workload((unknown,), _precision()).scheduled_chunk_count == 1


def test_url_query_and_fragment_not_misread_as_source_report_edition() -> None:
    url = "https://www.microsoft.com/investor/reports/ar24/index.html?from=/ar26/#fy2026"
    source = _document(1, _FINANCIAL_TABLE, url=url)
    assert plan_evidence_workload((source,), _precision()).scheduled_chunk_count == 1


def test_wrong_edition_on_preferred_domain_never_beats_eligible_source() -> None:
    wrong_edition = _document(1, _FINANCIAL_TABLE, url="https://www.microsoft.com/investor/reports/ar26/")
    correct_edition = _document(2, _FINANCIAL_TABLE, url="https://unlisted.example/opaque")
    result = plan_evidence_workload((wrong_edition, correct_edition), _precision(preferred_domains=("microsoft.com",)))
    assert result.scheduled_chunk_count == 1
    assert result.items[0].source_id == UUID(int=2)


def test_report_edition_cues_do_not_change_default_uuid_or_focus_modes() -> None:
    source = _document(1, _FINANCIAL_TABLE, url="https://www.microsoft.com/investor/reports/ar26/")
    for settings in (
        EvidenceWorkloadSettings(selection_mode="uuid", max_chunks_per_document=1),
        EvidenceWorkloadSettings(selection_mode="focus", focus_terms=("revenue",), max_chunks_per_document=1),
    ):
        assert plan_evidence_workload((source,), settings).scheduled_chunk_count == 1
