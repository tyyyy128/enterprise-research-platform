"""3I-1: real PostgreSQL + real extension wiring + deterministic external adapters.

Requires an explicitly opted-in LOCAL development database at Alembic head.
No GLM, Tavily, or public HTTP requests are issued: external adapter methods are
replaced, while plugin startup, HTTP API, services, Executor, fences, journal,
PostgreSQL persistence and run-report API are all production implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import Counter
from uuid import UUID

import pytest
from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps, HostPolicySnapshot
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from enterprise_research.application.claim_generation import ClaimCandidate
from enterprise_research.application.claim_verification import ClaimVerificationVerdict, EvidenceQuote
from enterprise_research.application.evidence_extraction import EvidenceCandidate
from enterprise_research.application.report_synthesis import ReportSynthesisPlan, ReportSynthesisSection
from enterprise_research.application.retrieval import WebFetchResult
from enterprise_research.application.search import SearchResult
from enterprise_research.domain import ClaimStatus, ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure.claim_generation import OpenAICompatibleClaimGenerator
from enterprise_research.infrastructure.claim_verification import OpenAICompatibleClaimVerifier
from enterprise_research.infrastructure.evidence_extraction import OpenAICompatibleEvidenceExtractor
from enterprise_research.infrastructure.planning import OpenAICompatibleResearchPlanner
from enterprise_research.infrastructure.postgres import (
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanQuestionRow,
    ResearchPlanRow,
    ResearchRunClaimGenerationRow,
    ResearchRunClaimVerificationRow,
    ResearchRunReportRow,
    ResearchRunReportSynthesisRow,
    ResearchRunRow,
    ResearchSourceDocumentRow,
    ResearchSourceRow,
    ResearchStageAttemptRow,
)
from enterprise_research.infrastructure.report_synthesis import OpenAICompatibleReportSynthesisPlanner
from enterprise_research.infrastructure.retrieval import TavilyExtractWebContentFetcher
from enterprise_research.infrastructure.searching import TavilySearchProvider
from enterprise_research.plugin import EnterpriseResearchService, build_router

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_E2E_TESTS") != "1",
    reason="3I-1 requires both explicit local-development PostgreSQL test switches.",
)

EXCERPT = "Revenue grew by 14 percent in 2025."
DOCUMENT = f"Company annual filing, published 2026: {EXCERPT} Audited primary-source finding."
SOURCE_URL = "https://example.test/company/annual-filing"
STAGES = {"planning", "searching", "extracting", "verifying", "writing"}


def _settings() -> PostgresSettings:
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("3I-1 tests require local enterprise_research development database, never remote/production.")
    return settings


def _configure(monkeypatch) -> None:
    """Enable all seven production adapters, with non-routable placeholder keys."""
    choices = {
        "ENTERPRISE_RESEARCH_REPOSITORY": "postgres",
        "ENTERPRISE_RESEARCH_GRAPH_BACKEND": "disabled",
        "ENTERPRISE_RESEARCH_PLANNER_BACKEND": "openai-compatible",
        "ENTERPRISE_RESEARCH_SEARCH_BACKEND": "tavily",
        "ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND": "tavily-extract",
        "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND": "openai-compatible",
        "ENTERPRISE_RESEARCH_CLAIM_BACKEND": "openai-compatible",
        "ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND": "openai-compatible",
        "ENTERPRISE_RESEARCH_WRITER_BACKEND": "openai-compatible",
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY": "1",
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS": "2",
    }
    for name, value in choices.items():
        monkeypatch.setenv(name, value)
    for prefix in ("PLANNER", "EVIDENCE", "CLAIM", "VERIFY", "SYNTHESIS"):
        monkeypatch.setenv(f"ENTERPRISE_RESEARCH_{prefix}_BASE_URL", "https://no-network.invalid/v1")
        monkeypatch.setenv(f"ENTERPRISE_RESEARCH_{prefix}_API_KEY", "never-used-test-secret")
        monkeypatch.setenv(f"ENTERPRISE_RESEARCH_{prefix}_MODEL", f"controlled-{prefix.lower()}")
    monkeypatch.setenv("TAVILY_API_KEY", "never-used-test-secret")


def _mock_external_adapters(monkeypatch, calls: list[str], *, outcome: str) -> None:
    """Patch the network boundary, not the stage runner, services or repositories."""

    async def plan(_self, *, task, run):
        calls.append("plan")
        return ResearchPlan(
            task_id=task.task_id,
            run_id=run.run_id,
            objective="Measure independently documented annual growth.",
            questions=[ResearchQuestion(question="What was annual revenue growth in 2025?")],
        )

    async def search(_self, query, *, max_results):
        calls.append("search")
        assert "growth" in query and max_results > 0
        if outcome == "no_search_results":
            return []
        return [
            SearchResult(
                title="Annual filing (controlled)",
                url=SOURCE_URL,
                snippet=EXCERPT,
                score=0.97,
                provider="controlled-search",
            )
        ]

    async def fetch(_self, url):
        calls.append("fetch")
        assert url == SOURCE_URL
        return WebFetchResult(
            requested_url=url,
            final_url=url,
            content=DOCUMENT,
            content_type="text/plain",
            metadata={"fixture": "3i-1"},
        )

    async def extract(_self, *, task, run, plan, source, chunk):
        calls.append("extract")
        assert source.url is not None and EXCERPT in chunk.content
        return [EvidenceCandidate(content=EXCERPT, relevance_score=0.99)]

    async def propose(_self, context):
        calls.append("propose")
        assert len(context.evidence) == 1
        return [ClaimCandidate(EXCERPT, (context.evidence[0].evidence_id,))]

    async def assess(_self, context):
        calls.append("assess")
        assert len(context.claims) == 1
        claim = context.claims[0]
        if outcome == "unsupported_claim":
            return [
                ClaimVerificationVerdict(
                    claim.claim_id, ClaimStatus.UNSUPPORTED, 0.2, "Evidence does not justify this claim."
                )
            ]
        return [
            ClaimVerificationVerdict(
                claim.claim_id,
                ClaimStatus.SUPPORTED,
                0.97,
                "The excerpt explicitly supports the candidate.",
                (EvidenceQuote(context.evidence[0].evidence_id, EXCERPT),),
            )
        ]

    async def compose(_self, context):
        calls.append("compose")
        assert len(context.claims) == 1
        claim_id = context.claims[0].claim_id
        return ReportSynthesisPlan(
            (claim_id,),
            (ReportSynthesisSection("Key findings", (claim_id,)),),
        )

    monkeypatch.setattr(OpenAICompatibleResearchPlanner, "plan", plan)
    monkeypatch.setattr(TavilySearchProvider, "search", search)
    monkeypatch.setattr(TavilyExtractWebContentFetcher, "fetch", fetch)
    monkeypatch.setattr(OpenAICompatibleEvidenceExtractor, "extract", extract)
    monkeypatch.setattr(OpenAICompatibleClaimGenerator, "propose", propose)
    monkeypatch.setattr(OpenAICompatibleClaimVerifier, "assess", assess)
    monkeypatch.setattr(OpenAICompatibleReportSynthesisPlanner, "compose", compose)


async def _wait_idle(service: EnterpriseResearchService, run_id: UUID) -> None:
    assert service._run_executor is not None
    tasks = service._run_executor._tasks
    task = tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=20)
    # The Executor callback removes the task as soon as it terminates.
    assert run_id not in tasks or tasks[run_id].done()


async def _rows(sessions, task_id: UUID, run_id: UUID) -> dict[str, list]:
    model_filters = {
        "plan": (ResearchPlanRow, ResearchPlanRow.run_id == run_id),
        "question": (
            ResearchPlanQuestionRow,
            ResearchPlanQuestionRow.plan_id.in_(select(ResearchPlanRow.id).where(ResearchPlanRow.run_id == run_id)),
        ),
        "source": (ResearchSourceRow, ResearchSourceRow.task_id == task_id),
        "document": (
            ResearchSourceDocumentRow,
            ResearchSourceDocumentRow.source_id.in_(
                select(ResearchSourceRow.id).where(ResearchSourceRow.task_id == task_id)
            ),
        ),
        "evidence": (ResearchEvidenceRow, ResearchEvidenceRow.task_id == task_id),
        "claim": (ResearchClaimRow, ResearchClaimRow.task_id == task_id),
        "link": (
            ClaimEvidenceRow,
            ClaimEvidenceRow.claim_id.in_(select(ResearchClaimRow.id).where(ResearchClaimRow.task_id == task_id)),
        ),
        "manifest": (ResearchEvidenceManifestRow, ResearchEvidenceManifestRow.run_id == run_id),
        "generation": (ResearchRunClaimGenerationRow, ResearchRunClaimGenerationRow.run_id == run_id),
        "verification": (ResearchRunClaimVerificationRow, ResearchRunClaimVerificationRow.run_id == run_id),
        "report": (ResearchRunReportRow, ResearchRunReportRow.run_id == run_id),
        "synthesis": (ResearchRunReportSynthesisRow, ResearchRunReportSynthesisRow.run_id == run_id),
        "journal": (ResearchStageAttemptRow, ResearchStageAttemptRow.run_id == run_id),
    }
    async with sessions() as session:
        result = {}
        for name, (model, condition) in model_filters.items():
            result[name] = list((await session.scalars(select(model).where(condition))).all())
        result["run"] = [await session.get(ResearchRunRow, run_id)]
        return result


@pytest.mark.parametrize("outcome", ["completed", "unsupported_claim", "no_search_results"])
def test_full_http_to_terminal_pipeline_with_controlled_adapters(monkeypatch, outcome):
    """Run through the *real* plugin API, Executor, stages and DB proofs."""
    _settings()  # Check the target BEFORE configuring anything that writes.
    _configure(monkeypatch)
    calls: list[str] = []
    _mock_external_adapters(monkeypatch, calls, outcome=outcome)

    async def exercise():
        app = FastAPI()
        service = EnterpriseResearchService()
        app.include_router(build_router(service))
        task_id = None
        try:
            await service.start(
                ExtensionRuntimeDeps(
                    app_store=ExtensionData("3i-1-e2e"),
                    policy=HostPolicySnapshot(max_subagents_per_run=3),
                    session_factory=object(),
                )
            )
            assert service._engine is not None
            sessions = build_session_factory(service._engine)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://controlled.test") as client:
                created = await client.post(
                    "/api/enterprise-research/tasks", json={"query": "What was annual revenue growth in 2025?"}
                )
                assert created.status_code == 201, created.text
                task_id = UUID(created.json()["task_id"])
                started = await client.post(f"/api/enterprise-research/tasks/{task_id}/runs")
                assert started.status_code == 201, started.text
                run_id = UUID(started.json()["run_id"])
                await _wait_idle(service, run_id)

                status_response = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}")
                assert status_response.status_code == 200
                run = status_response.json()
                events = await asyncio.wait_for(
                    client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}/events"),
                    timeout=5,
                )
                assert events.status_code == 200
                assert "event: run.created" in events.text
                assert "event: run.stage" in events.text
                assert ("event: run.completed" if outcome == "completed" else "event: run.failed") in events.text
                rows = await _rows(sessions, task_id, run_id)
                assert rows["run"][0].status == run["status"]
                assert len(rows["plan"]) == len(rows["question"]) == 1
                assert set(row.stage for row in rows["journal"]) == (
                    STAGES if outcome != "no_search_results" else {"planning", "searching"}
                )
                assert len(rows["journal"]) == len(set((row.stage, row.fencing_token) for row in rows["journal"]))
                assert len({row.fencing_token for row in rows["journal"]}) == 1

                report_response = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}/report")
                if outcome == "completed":
                    assert run["status"] == ResearchRunStatus.COMPLETED
                    assert all(row.outcome == "completed" for row in rows["journal"])
                    assert all(
                        len(rows[key]) == 1
                        for key in (
                            "source",
                            "document",
                            "evidence",
                            "claim",
                            "link",
                            "manifest",
                            "generation",
                            "verification",
                            "report",
                            "synthesis",
                        )
                    )
                    assert rows["claim"][0].status == "supported"
                    assert str(rows["claim"][0].id) in rows["generation"][0].claim_ids
                    assert str(rows["claim"][0].id) in rows["verification"][0].claim_ids
                    assert rows["link"][0].evidence_id == rows["evidence"][0].id
                    assert str(rows["evidence"][0].id) in rows["manifest"][0].evidence_ids
                    assert report_response.status_code == 200, report_response.text
                    report = report_response.json()
                    assert EXCERPT in report["body"]
                    assert len(report["citations"]) == 1
                    assert report["body_sha256"] == hashlib.sha256(report["body"].encode()).hexdigest()
                    assert report["synthesis_model"] == "controlled-synthesis"
                    assert len(report["synthesis_sha256"]) == 64
                    assert Counter(calls) == Counter(
                        {
                            "plan": 1,
                            "search": 1,
                            "fetch": 1,
                            "extract": 1,
                            "propose": 1,
                            "assess": 1,
                            "compose": 1,
                        }
                    )
                else:
                    assert run["status"] == ResearchRunStatus.FAILED
                    assert run["error"]
                    assert report_response.status_code == 409
                    assert not rows["report"] and not rows["synthesis"]
                    assert rows["journal"][-1].outcome == "failed" or any(
                        row.outcome == "failed" for row in rows["journal"]
                    )
                    if outcome == "no_search_results":
                        assert not rows["source"] and not rows["document"]
                        assert not rows["manifest"] and not rows["generation"]
                        assert Counter(calls) == Counter({"plan": 1, "search": 1})
                    else:
                        assert len(rows["verification"]) == 1
                        assert rows["claim"][0].status == "unsupported"
                        assert all(row.outcome == "completed" for row in rows["journal"] if row.stage != "writing")
                        assert Counter(calls) == Counter(
                            {
                                "plan": 1,
                                "search": 1,
                                "fetch": 1,
                                "extract": 1,
                                "propose": 1,
                                "assess": 1,
                            }
                        )
        finally:
            await service.stop()
            if task_id is not None:
                cleanup_engine = build_async_engine(_settings())
                try:
                    cleanup_sessions = build_session_factory(cleanup_engine)
                    async with cleanup_sessions() as session:
                        async with session.begin():
                            await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task_id))
                finally:
                    await cleanup_engine.dispose()

    asyncio.run(exercise())
