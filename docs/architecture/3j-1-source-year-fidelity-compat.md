# 3J-1 Source-Year Fidelity Compatibility Hotfix

## Context

The FY2024 Total revenue result was numerically correct, but the first live
run cited an FY2026-edition comparative statement (`2026 2025 2024`) rather
than the original FY2024 statement (`2024 2023 2022`). The report must never
claim source-year fidelity merely because a historical column happens to match.

## Current-worktree compatibility

This increment is based on the *user-uploaded, post-leading-column* five-file
worktree snapshot. That tree already contained leading-column protection and
per-chunk tier >= 3 filtering, so neither is re-applied here.

Additional fail-closed checks, only in explicit `financial_precision` mode:

1. The primary income-statement years must be strictly descending, with the
   requested fiscal year first, in both multiline and whitespace-flattened text.
2. An explicitly different document edition in an `ar24` / `ar2026` /
   `fy2026` HTTP(S) URL **path** or bounded first-chunk annual-report cover
   disqualifies the entire document; conflicting edition clues also reject it.
3. Unknown CDN UUID URLs do not become a guessed edition. They still need a
   qualifying first-column income-statement excerpt.
4. The existing per-chunk tier gate, unmodified original source offsets,
   scheduling budgets and default `uuid`/`focus` modes remain unchanged.

These lexical checks are necessary but not a complete provenance audit.
A fabricated or reprinted FY2024-first table on an unknown URL cannot be
proven original by regex alone. Report authority and provenance remain
separate final-acceptance concerns.

## Acceptance

- Offline tests: verify the date order, URL/cover conflict rejection,
  FY2024 original acceptance, UUID CDN acceptance, preferred-domain behavior,
  and mode isolation.
- Re-run the user's WSL2 focused and full pytest suites plus Ruff checks.
- Only then run a **read-only/no-provider** replay on previously saved FY2026
  documents. Do not repeat the live billed Run or commit changes.

The patch does not improve Tavily search discovery of the genuine FY2024
annual report; that is a separate task.
