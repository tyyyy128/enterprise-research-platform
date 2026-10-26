# 3J-2 Source-Year-Aware Annual-Report Discovery

## Scope and safety boundary

This is an opt-in discovery enhancement for the existing `financial_precision`
Total-revenue workload. It does **not** change evidence admission, extraction,
run state, persistence schemas, or the Tavily request budget.

- When `ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE=financial_precision`, the
  current search service derives the target year from validated existing
  `ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS` and reuses the optional
  `ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS` list.
- Each planned question still triggers **one** provider search with the same
  `max_results` bound. A bounded deterministic query prefix requests the
  original target-year annual report and its income statements.
- Results whose URL path or title **explicitly** declares another annual-report
  edition (including conflicting hints) are discarded. A lack of an explicit
  edition cue remains **unknown**, not verified.
- Remaining results are ranked by HTTPS preferred-hostname match, then
  matching report-edition cue, retaining provider order on ties. Preferred
  domains are validated by DNS labels, not string containment.
- Run-scoped search hit metadata includes the actual query, target year,
  `matching`/`unknown` edition **hint**, and whether the URL matches a preferred
  host. The hint never certifies source integrity.
- If a planned question has no remaining candidates, search fails without
  persisting any staged partial sources, preserving the existing Safe Resume
  guarantee. No retry or fallback search is performed.
- All non-financial search runs use the original query, result ordering, and
  persistence behavior.

## Distinguish discovery from evidence

URL/title edition hints are untrusted metadata. Unknown-version CDN results can
proceed to retrieval but are not automatically acceptable as financial evidence.
The 3J-1 source-year and per-chunk financial precision checks remain the
independent gate. A search result alone must never mark the report or task
COMPLETED.

This patch does **not** verify original-page publication dates, detect every
possible PDF/HTML title format, guarantee a preferred domain returns useful
results, or prove transaction atomicity in production PostgreSQL.

## Primary FY2024 reference (fixture only, not a live provider call)

- Official report entry: `https://www.microsoft.com/investor/reports/ar24/index.html`
- Income-statement columns: `2024 / 2023 / 2022`
- Total revenue, USD millions: `245,122 / 211,915 / 198,270`

The actual source must still be retrieved, chunked, and checked by 3J-1.
Neither fixed result fixtures nor the official URL constitute a live search
acceptance test.

## Offline verification

The patch adds tests to `tests/application/test_financial_source_discovery.py`
and `tests/application/test_search_service.py` for query bounds, source version
mismatches, title/URL conflicts, preferred-hostname spoofing, unknown editions,
provider request count, failure-without-persistence and repeat-search idempotence.

Before applying, stop if `git apply --check` reports a mismatch; never force
an overwrite or discard existing work. Run the targeted tests, Ruff and full
regression suite inside the original WSL2 project. Do not use GLM/Tavily for
this stage's acceptance.
