# Enterprise Deep Research & Knowledge Agent Platform

> Evidence-grounded research workflows with source-year fidelity, provenance controls, and fail-closed execution.

**Status:** v0.1.0 Preview — research / engineering prototype; not production-certified.

[中文说明](README.zh-CN.md)

## Overview

An independent enterprise-research extension for the DeerFlow ecosystem. The project builds a structured research workflow around planning, searching, retrieval, evidence extraction, claim verification, and report generation.

It extends generic agent orchestration with **research-specific evidence admission and auditability**. It is an extension, not a standalone DeerFlow distribution.

## Highlights

- **Evidence provenance:** source → retrieved document → chunk → evidence → claim → report.
- **Source-year fidelity:** when an original FY2024 annual report is required, a FY2026 report's historical comparison column is not accepted as an FY2024 original source.
- **Fail-closed execution:** when evidence does not meet strict criteria, the workflow must reject the evidence rather than silently complete a report.
- **Year-aware discovery:** improve annual-report search queries, reject explicitly wrong report editions, prioritize official domains, and keep unknown editions unverified.
- **Cost-bounded research:** explicit limits for search results and evidence chunks; deterministic offline replay tests.
- **Run lifecycle:** PostgreSQL-backed research artifacts, run stages and events, with database-specific tests guarded by explicit opt-ins.

## Architecture (conceptual)

```text
Research task
  -> Planning
  -> Search / Source-Year-Aware Discovery
  -> Retrieval / Documents / Chunks
  -> Evidence Admission / Extraction
  -> Claim Generation / Verification
  -> Report Synthesis / Writing
  -> Run State + Stage Journal + SSE
```

Important implementation modules:

| Module | Purpose |
| --- | --- |
| `src/enterprise_research/application/financial_source_discovery.py` | Fiscal-year-aware discovery, version hints, candidate ranking |
| `src/enterprise_research/application/search_service.py` | Search workflow, source selection and persistence |
| `src/enterprise_research/application/financial_evidence_scoring.py` | Fiscal-year table and report edition validation |
| `src/enterprise_research/application/evidence_workload.py` | Document/chunk admission and work budgets |
| `src/enterprise_research/application/evidence_extraction_service.py` | Evidence extraction and fail-closed boundary |
| `src/enterprise_research/plugin.py` | DeerFlow extension service and HTTP API assembly |

## Requirements & setup

- Python **3.12+**
- A compatible **DeerFlow extension runtime** providing `deerflow_extension_api`
- The Python dependencies declared in `pyproject.toml`
- PostgreSQL only for modes requiring persistent storage
- Optional external providers (Tavily / OpenAI-compatible endpoints) only when explicitly enabled

**Integration caveat:** public fresh-clone installation and runtime compatibility have not yet been independently verified. Before marking an install path as supported, document the exact compatible DeerFlow version and confirm that all dependencies in `pyproject.toml` are obtainable in a clean environment.

In an existing configured development environment, the regression suite is run with:

```bash
uv run --no-project pytest -q
```

The local baseline reported during development was **664 passed, 63 skipped**. Most skipped tests require explicitly enabled, migrated PostgreSQL development test databases. This result is not a clean-clone CI guarantee.

## Verification boundaries

- Source-year admission and Worker fail-closed behavior were replayed against previously persisted historical documents, using a read-only database connection and in-memory workflow state.
- Source-year-aware discovery was tested against **synthetic fixed search candidates**. This **does not prove** that a live search provider returned the correct FY2024 annual report.
- Full PostgreSQL transaction atomicity, live discovery quality, public fresh-clone setup and production security have not been completely validated.

## Security

Never commit `.env` files, API credentials, database dumps, historical private research data, provider response bodies, or private key material. Review [`SECURITY.md`](SECURITY.md) before publishing.

## Roadmap

1. Verify historical real search-result candidates (3J-2C).
2. Complete an eligible original-source end-to-end positive case (3J-2D).
3. Improve isolated PostgreSQL integration checks and clean-clone CI.
4. Run a separately approved, budget-limited live-provider acceptance test.

## Attribution and license

Built as an independent extension for DeerFlow. DeerFlow is a separate upstream project; refer to its upstream repository for its license and notices.

**License decision pending:** do not label this repository as MIT-licensed until ownership, copied components, dependency notices, and the intended project license are reviewed.
