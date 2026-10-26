"""3I-2: opt-in live GLM/Tavily HTTP -> Executor -> PostgreSQL acceptance.

Never enable this module during normal extension startup. By default the CLI
performs LOCAL/OFFLINE environment preflight and makes no external requests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

MODEL_STAGES = ("PLANNER", "EVIDENCE", "CLAIM", "VERIFY", "SYNTHESIS")
REQUIRED_BACKENDS = {
    "ENTERPRISE_RESEARCH_REPOSITORY": "postgres",
    "ENTERPRISE_RESEARCH_PLANNER_BACKEND": "openai-compatible",
    "ENTERPRISE_RESEARCH_SEARCH_BACKEND": "tavily",
    "ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND": "tavily-extract",
    "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND": "openai-compatible",
    "ENTERPRISE_RESEARCH_CLAIM_BACKEND": "openai-compatible",
    "ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND": "openai-compatible",
    "ENTERPRISE_RESEARCH_WRITER_BACKEND": "openai-compatible",
    "ENTERPRISE_RESEARCH_GRAPH_BACKEND": "disabled",
}
SMALL_WORKLOAD = {
    "ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS": "1",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_DOCUMENTS": "1",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CHUNKS_PER_DOCUMENT": "1",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS": "1",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY": "1",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CANDIDATES_PER_CHUNK": "2",
    "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS": "2500",
    "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS": "200",
    "ENTERPRISE_RESEARCH_EVIDENCE_MAX_EXCERPT_CHARS": "600",
    "ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS": "12000",
    "ENTERPRISE_RESEARCH_CLAIM_MAX_EXCERPT_CHARS": "600",
    "ENTERPRISE_RESEARCH_VERIFY_MAX_EXCERPT_CHARS": "600",
    "ENTERPRISE_RESEARCH_CLAIM_MAX_EVIDENCE": "4",
    "ENTERPRISE_RESEARCH_CLAIM_MAX_CANDIDATES": "2",
    "ENTERPRISE_RESEARCH_CLAIM_MAX_REFERENCES_PER_CLAIM": "2",
    "ENTERPRISE_RESEARCH_VERIFY_MAX_EVIDENCE": "4",
    "ENTERPRISE_RESEARCH_VERIFY_MAX_CLAIMS": "2",
}
TERMINAL = {"completed", "failed"}


class LiveAcceptanceError(RuntimeError):
    """Fail-safe refusal or live-run problem; never carries credentials."""


class RequestBudgetExceeded(LiveAcceptanceError):
    """No further external requests are allowed."""


@dataclass(frozen=True)
class LiveBudget:
    max_requests: int = 16
    max_model_requests: int = 10
    max_tavily_requests: int = 6
    max_output_tokens_per_request: int = 2048
    timeout_seconds: int = 420
    max_model_request_bytes: int = 32_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_requests <= 100:
            raise ValueError("max_requests must be within 1..100")
        if not 1 <= self.max_model_requests <= self.max_requests:
            raise ValueError("max_model_requests must be within 1..max_requests")
        if not 1 <= self.max_tavily_requests <= self.max_requests:
            raise ValueError("max_tavily_requests must be within 1..max_requests")
        if not 128 <= self.max_output_tokens_per_request <= 8192:
            raise ValueError("max_output_tokens_per_request must be within 128..8192")
        if not 30 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be within 30..3600")
        if not 1024 <= self.max_model_request_bytes <= 1_000_000:
            raise ValueError("max_model_request_bytes must be within 1024..1000000")


def _https_endpoint(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in raw)
    ):
        raise LiveAcceptanceError("Live model endpoints require clean HTTPS URLs without credentials or queries")
    return raw.rstrip("/")


def endpoint_inventory(env: dict[str, str]) -> dict[str, str]:
    """Exact outbound allowlist; never log origins or raw endpoint paths."""
    endpoints = {
        "https://api.tavily.com/search": "tavily_search",
        "https://api.tavily.com/extract": "tavily_extract",
    }
    retrieval = env.get("ENTERPRISE_RESEARCH_RETRIEVAL_BASE_URL", "https://api.tavily.com").rstrip("/")
    if retrieval != "https://api.tavily.com":
        raise LiveAcceptanceError("Live acceptance permits only the official Tavily Extract endpoint")
    for stage in MODEL_STAGES:
        prefix = f"ENTERPRISE_RESEARCH_{stage}"
        base_url = _https_endpoint(env[f"{prefix}_BASE_URL"])
        endpoint = f"{base_url}/chat/completions"
        if endpoint in endpoints and endpoints[endpoint] != "model":
            raise LiveAcceptanceError("Model endpoint collides with a Tavily endpoint")
        endpoints[endpoint] = "model"
    return endpoints


def preflight(env: dict[str, str]) -> list[str]:
    """Pure local checks. Does NOT connect to a database, provider, or URL."""
    problems: list[str] = []
    if env.get("ENTERPRISE_RESEARCH_DB_HOST", "127.0.0.1") not in {"localhost", "127.0.0.1", "::1"}:
        problems.append("DB host must be localhost/127.0.0.1/::1")
    if env.get("ENTERPRISE_RESEARCH_DB_NAME") != "enterprise_research":
        problems.append("DB name must be enterprise_research")
    for suffix in ("DB_USER", "DB_PASSWORD"):
        if not env.get(f"ENTERPRISE_RESEARCH_{suffix}", "").strip():
            problems.append(f"Missing ENTERPRISE_RESEARCH_{suffix}")
    if not env.get("TAVILY_API_KEY", "").strip():
        problems.append("Missing TAVILY_API_KEY")
    for stage in MODEL_STAGES:
        for suffix in ("BASE_URL", "API_KEY", "MODEL"):
            name = f"ENTERPRISE_RESEARCH_{stage}_{suffix}"
            if not env.get(name, "").strip():
                problems.append(f"Missing {name}")
    if not problems:
        try:
            endpoint_inventory(env)
        except (KeyError, LiveAcceptanceError) as exc:
            problems.append(str(exc) if not isinstance(exc, KeyError) else "Model endpoint config is incomplete")
    return problems


def _apply_output_token_cap(request: httpx.Request, cap: int) -> httpx.Request:
    """Bound response generation while preserving production adapter payloads."""
    try:
        body = json.loads(request.content)
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise ValueError("invalid model request")
    except (ValueError, TypeError) as exc:
        raise LiveAcceptanceError("Refusing model request with an unexpected body") from exc
    current = body.get("max_tokens")
    body["max_tokens"] = min(current, cap) if isinstance(current, int) and current > 0 else cap
    # The original JSON body has a different Content-Length. Let httpx
    # calculate the correct framing header for the rewritten payload.
    headers = httpx.Headers(request.headers)
    headers.pop("content-length", None)
    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=headers,
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        extensions=request.extensions,
    )


@dataclass
class OutboundCall:
    kind: str
    duration_seconds: float
    status_code: int | None = None
    failure_type: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tavily_credits: float | None = None


class OutboundBudgetGate:
    """Intercept ALL external httpx sends, including failed attempts, before network I/O.

    ASGITransport is the known in-process HTTP boundary. All other destinations
    must match one of the exact configured provider endpoints.
    """

    def __init__(self, budget: LiveBudget, endpoints: dict[str, str]) -> None:
        self.budget = budget
        self.endpoints = endpoints
        self.started_at = time.monotonic()
        self.calls: list[OutboundCall] = []
        self.attempts: Counter[str] = Counter()
        self.blocked = 0
        self._lock = asyncio.Lock()

    async def _reserve(self, request: httpx.Request) -> str:
        kind = self.endpoints.get(str(request.url))
        if request.method != "POST" or kind is None:
            self.blocked += 1
            raise LiveAcceptanceError("Blocked unapproved outbound HTTP destination or method")
        if kind == "model" and len(request.content) > self.budget.max_model_request_bytes:
            self.blocked += 1
            raise RequestBudgetExceeded("Model request input-size budget exhausted")
        async with self._lock:
            model_count = self.attempts["model"]
            tavily_count = self.attempts["tavily_search"] + self.attempts["tavily_extract"]
            if (
                sum(self.attempts.values()) >= self.budget.max_requests
                or (kind == "model" and model_count >= self.budget.max_model_requests)
                or (kind.startswith("tavily") and tavily_count >= self.budget.max_tavily_requests)
                or time.monotonic() - self.started_at >= self.budget.timeout_seconds
            ):
                self.blocked += 1
                raise RequestBudgetExceeded("External request-count or runtime budget exhausted")
            self.attempts[kind] += 1
        return kind

    @contextmanager
    def install(self):
        original = httpx.AsyncClient.send
        gate = self

        async def guarded_send(client, request, *args, **kwargs):
            if isinstance(client._transport, httpx.ASGITransport):
                return await original(client, request, *args, **kwargs)
            kind = await gate._reserve(request)
            if kind == "model":
                request = _apply_output_token_cap(request, gate.budget.max_output_tokens_per_request)
            start = time.monotonic()
            record = OutboundCall(kind=kind, duration_seconds=0.0)
            try:
                response = await original(client, request, *args, **kwargs)
                record.status_code = response.status_code
                try:
                    content_bytes = response.content
                except httpx.ResponseNotRead:
                    content_bytes = b""
                if content_bytes and len(content_bytes) <= 2_000_000:
                    try:
                        data = response.json()
                        if isinstance(data, dict):
                            usage = data.get("usage")
                            if isinstance(usage, dict):
                                if kind == "model":
                                    for field_name in ("prompt_tokens", "completion_tokens"):
                                        value = usage.get(field_name)
                                        if type(value) is int and value >= 0:
                                            setattr(record, field_name, value)
                                else:
                                    credits = usage.get("credits")
                                    if isinstance(credits, (float, int)) and not isinstance(credits, bool):
                                        record.tavily_credits = float(credits)
                    except (ValueError, TypeError):
                        pass
                return response
            except BaseException as exc:
                record.failure_type = type(exc).__name__
                raise
            finally:
                record.duration_seconds = round(time.monotonic() - start, 4)
                gate.calls.append(record)

        httpx.AsyncClient.send = guarded_send
        try:
            yield self
        finally:
            httpx.AsyncClient.send = original

    def summary(self) -> dict[str, Any]:
        model_calls = [call for call in self.calls if call.kind == "model"]
        known_tokens = [
            call for call in model_calls if call.prompt_tokens is not None and call.completion_tokens is not None
        ]
        return {
            "requests_started": sum(self.attempts.values()),
            "requests_by_kind": dict(self.attempts),
            "requests_blocked": self.blocked,
            "model_usage_observed": len(known_tokens),
            "model_calls_recorded": len(model_calls),
            "prompt_tokens_observed": sum(c.prompt_tokens for c in known_tokens),
            "completion_tokens_observed": sum(c.completion_tokens for c in known_tokens),
            "tavily_credits_observed": round(sum(c.tavily_credits or 0 for c in self.calls), 4),
            "per_request": [asdict(call) for call in self.calls],
            "limits": asdict(self.budget),
            "billing_note": (
                "Provider invoices are authoritative. Missing usage is not zero; HTTP cap is not a dollar cap."
            ),
        }


def classify_failure(message: str | None) -> str | None:
    """Store a small reason category, never the potentially sensitive error body."""
    if not message:
        return None
    value = message.casefold()
    if "budget" in value or "unapproved outbound" in value:
        return "budget_or_network_gate"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    if "lease" in value or "fencing" in value:
        return "execution_authority"
    if "search" in value or "tavily" in value:
        return "search_or_retrieval"
    if "claim" in value or "verification" in value:
        return "claim_or_verification"
    if "evidence" in value:
        return "evidence"
    if "writing" in value or "report" in value:
        return "report_synthesis"
    if "plan" in value:
        return "planning"
    return "other"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def audit_snapshot(
    *,
    task_id: UUID,
    run_id: UUID,
    run: Any,
    plan: Any,
    questions: list[Any],
    sources: list[Any],
    documents: list[Any],
    manifest: Any,
    generation: Any,
    verification: Any,
    claims: list[Any],
    evidence_rows: list[Any],
    claim_links: list[Any],
    report: Any,
    synthesis: Any,
    journal: list[Any],
) -> dict[str, Any]:
    """Mechanical research quality *proxies*; never assert semantic correctness."""
    question_ids = {str(row.id) for row in questions}
    covered: set[str] = set()
    run_sources = []
    for source in sources:
        metadata = source.source_metadata if isinstance(source.source_metadata, dict) else {}
        search = metadata.get("search", {})
        hits = search.get("hits", []) if isinstance(search, dict) else []
        if any(isinstance(hit, dict) and hit.get("run_id") == str(run_id) for hit in hits):
            run_sources.append(source)
        covered.update(
            str(hit["question_id"])
            for hit in hits
            if isinstance(hit, dict)
            and hit.get("run_id") == str(run_id)
            and str(hit.get("question_id")) in question_ids
        )
    source_by_id = {str(source.id): source for source in run_sources}
    success_docs = [doc for doc in documents if str(doc.source_id) in source_by_id and doc.status == "succeeded"]
    evidence_ids = set(manifest.evidence_ids) if manifest else set()
    candidate_ids = set(generation.claim_ids) if generation else set()
    statuses = Counter(claim.status for claim in claims if str(claim.id) in candidate_ids)
    supported_claim_ids = {str(c.id) for c in claims if str(c.id) in candidate_ids and c.status == "supported"}
    evidence_by_id = {str(e.id): e for e in evidence_rows if str(e.id) in evidence_ids}
    linked_pairs = {(str(link.claim_id), str(link.evidence_id)) for link in claim_links}

    valid_citations = 0
    citation_count = 0
    if report is not None:
        for citation in report.citations:
            if not isinstance(citation, dict):
                continue
            claim_id = citation.get("claim_id")
            for evidence in citation.get("evidence", []):
                citation_count += 1
                if not isinstance(evidence, dict):
                    continue
                evid = evidence.get("evidence_id")
                src = source_by_id.get(evidence.get("source_id"))
                original = evidence_by_id.get(evid)
                if original is None:
                    continue
                payload = json.dumps(
                    [str(original.source_id), original.locator, original.content],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if (
                    claim_id in supported_claim_ids
                    and (claim_id, evid) in linked_pairs
                    and src is not None
                    and original.source_id == src.id
                    and evidence.get("url") == src.url
                    and evidence.get("fingerprint") == fingerprint
                    and getattr(manifest, "evidence_hashes", {}).get(evid) == fingerprint
                ):
                    valid_citations += 1
    body_hash_valid = (
        hashlib.sha256(report.body.encode("utf-8")).hexdigest() == report.body_sha256 if report is not None else None
    )
    stage_rows = [
        {
            "stage": attempt.stage,
            "outcome": attempt.outcome,
            "duration_seconds": round((attempt.finished_at - attempt.started_at).total_seconds(), 4)
            if attempt.finished_at is not None
            else None,
            "fencing_token": attempt.fencing_token,
        }
        for attempt in journal
    ]
    return {
        "status": run.status,
        "failure_category": classify_failure(run.error),
        "failure_message_sha256": hashlib.sha256(run.error.encode("utf-8")).hexdigest() if run.error else None,
        "stage_journal": stage_rows,
        "artifacts": {
            "plan_present": plan is not None,
            "planned_questions": len(questions),
            "question_search_coverage": _ratio(len(covered), len(question_ids)),
            "run_search_sources": len(run_sources),
            "retrieved_successful_sources": len(success_docs),
            "retrieval_success_ratio": _ratio(len(success_docs), len(run_sources)),
            "evidence_manifest_present": manifest is not None,
            "run_evidence_count": len(evidence_ids),
            "generation_receipt_present": generation is not None,
            "candidate_claim_count": len(candidate_ids),
            "verification_receipt_present": verification is not None,
            "verified_supported": statuses["supported"],
            "verified_unsupported": statuses["unsupported"],
            "verified_conflicted": statuses["conflicted"],
            "report_present": report is not None,
            "report_sha256_valid": body_hash_valid,
            "report_citations": citation_count,
            "mechanically_verified_citation_ratio": _ratio(valid_citations, citation_count),
            "synthesis_receipt_present": synthesis is not None,
        },
        "review_required": [
            "Evidence excerpts require manual source comparison",
            "Claim semantic support/contradiction must be independently reviewed",
            "Question coverage is a search-hit proxy, not full factual answer coverage",
            "Report coherence and conflict treatment require human review",
        ],
    }


async def collect_snapshot(sessions, task_id: UUID, run_id: UUID) -> dict[str, Any]:
    """Read only the new task/run's persistence; do not modify user data."""
    from sqlalchemy import select

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

    async with sessions() as session:
        run = await session.get(ResearchRunRow, run_id)
        if run is None or run.task_id != task_id:
            raise LiveAcceptanceError("Research run was not persisted for this task")
        plan = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id))
        questions = []
        if plan is not None:
            questions = list(
                (
                    await session.scalars(
                        select(ResearchPlanQuestionRow).where(ResearchPlanQuestionRow.plan_id == plan.id)
                    )
                ).all()
            )
        sources = list(
            (await session.scalars(select(ResearchSourceRow).where(ResearchSourceRow.task_id == task_id))).all()
        )
        documents = []
        if sources:
            documents = list(
                (
                    await session.scalars(
                        select(ResearchSourceDocumentRow).where(
                            ResearchSourceDocumentRow.source_id.in_([source.id for source in sources])
                        )
                    )
                ).all()
            )
        generation = await session.get(ResearchRunClaimGenerationRow, run_id)
        claims = []
        claim_links = []
        if generation is not None and generation.claim_ids:
            candidate_ids = [UUID(value) for value in generation.claim_ids]
            claims = list(
                (
                    await session.scalars(
                        select(ResearchClaimRow).where(
                            ResearchClaimRow.id.in_(candidate_ids), ResearchClaimRow.task_id == task_id
                        )
                    )
                ).all()
            )
            claim_links = list(
                (
                    await session.scalars(select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id.in_(candidate_ids)))
                ).all()
            )
        manifest = await session.get(ResearchEvidenceManifestRow, run_id)
        evidence_rows = []
        if manifest is not None and manifest.evidence_ids:
            evidence_rows = list(
                (
                    await session.scalars(
                        select(ResearchEvidenceRow).where(
                            ResearchEvidenceRow.id.in_([UUID(value) for value in manifest.evidence_ids]),
                            ResearchEvidenceRow.task_id == task_id,
                        )
                    )
                ).all()
            )
        journal = list(
            (
                await session.scalars(
                    select(ResearchStageAttemptRow)
                    .where(ResearchStageAttemptRow.run_id == run_id)
                    .order_by(ResearchStageAttemptRow.started_at)
                )
            ).all()
        )
        return audit_snapshot(
            task_id=task_id,
            run_id=run_id,
            run=run,
            plan=plan,
            questions=questions,
            sources=sources,
            documents=documents,
            manifest=manifest,
            generation=generation,
            verification=await session.get(ResearchRunClaimVerificationRow, run_id),
            claims=claims,
            evidence_rows=evidence_rows,
            claim_links=claim_links,
            report=await session.get(ResearchRunReportRow, run_id),
            synthesis=await session.get(ResearchRunReportSynthesisRow, run_id),
            journal=journal,
        )


@contextmanager
def temporary_workload_env(env: dict[str, str]):
    """Bounded acceptance config; restores caller's original process environment."""
    from enterprise_research.acceptance.financial_workload import financial_workload_overrides

    overrides = {**REQUIRED_BACKENDS, **SMALL_WORKLOAD}
    overrides.update(financial_workload_overrides(env))
    previous = {key: env.get(key) for key in overrides}
    try:
        env.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value


def _write_private(path: Path, content: str) -> None:
    """Private, atomic result files; never include credentials."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        handle.write(content)
    os.replace(tmp, path)


async def execute_live(query: str, output_dir: Path, budget: LiveBudget, *, save_report: bool) -> tuple[Path, dict]:
    """Execute exactly one new task/run; no automatic retry or failed-run cleanup."""
    from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps, HostPolicySnapshot
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from enterprise_research.infrastructure.postgres import build_session_factory
    from enterprise_research.plugin import EnterpriseResearchService, build_router

    env = dict(os.environ)
    from enterprise_research.acceptance.financial_workload import financial_workload_issues

    problems = [*preflight(env), *financial_workload_issues(env)]
    if problems:
        raise LiveAcceptanceError("Preflight failed: " + "; ".join(problems))
    service = EnterpriseResearchService()
    api = FastAPI()
    api.include_router(build_router(service))
    task_id: UUID | None = None
    run_id: UUID | None = None
    start = time.monotonic()
    gate = OutboundBudgetGate(budget, endpoint_inventory(env))
    result: dict[str, Any] = {
        "harness": "3I-2",
        "started_utc": datetime.now(UTC).isoformat(),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "privacy": "No raw request, response, secret, or research query is included",
        "timed_out": False,
        "status": "harness_error",
    }
    try:
        with temporary_workload_env(os.environ), gate.install():
            try:
                await service.start(
                    ExtensionRuntimeDeps(
                        app_store=ExtensionData("3i-2-live-acceptance"),
                        policy=HostPolicySnapshot(max_subagents_per_run=3),
                        session_factory=object(),
                    )
                )
                async with AsyncClient(transport=ASGITransport(app=api), base_url="http://acceptance.local") as client:
                    created = await client.post("/api/enterprise-research/tasks", json={"query": query})
                    if created.status_code != 201:
                        raise LiveAcceptanceError(f"Create task rejected with HTTP {created.status_code}")
                    task_id = UUID(created.json()["task_id"])
                    started = await client.post(f"/api/enterprise-research/tasks/{task_id}/runs")
                    if started.status_code != 201:
                        raise LiveAcceptanceError(f"Create run rejected with HTTP {started.status_code}")
                    run_id = UUID(started.json()["run_id"])
                    print(f"Live task created: task_id={task_id} run_id={run_id}", flush=True)

                    async def await_terminal():
                        while True:
                            snapshot = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}")
                            if snapshot.status_code != 200:
                                raise LiveAcceptanceError(f"Run API failed with HTTP {snapshot.status_code}")
                            state = snapshot.json()
                            if state["status"] in TERMINAL:
                                return state
                            await asyncio.sleep(0.5)

                    try:
                        state = await asyncio.wait_for(await_terminal(), timeout=budget.timeout_seconds)
                    except TimeoutError:
                        result["timed_out"] = True
                        state = {"status": "timeout"}
                    result["status"] = state["status"]
                    if state["status"] in TERMINAL:
                        try:
                            sse_response = await asyncio.wait_for(
                                client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}/events"),
                                timeout=5,
                            )
                            terminal_name = "run.completed" if state["status"] == "completed" else "run.failed"
                            result["sse_terminal_observed"] = (
                                sse_response.status_code == 200 and f"event: {terminal_name}" in sse_response.text
                            )
                            result["sse_stage_event_count"] = sse_response.text.count("event: run.stage")
                        except TimeoutError:
                            result["sse_terminal_observed"] = False
                            result["sse_stage_event_count"] = None
                    if state["status"] == "timeout":
                        result["harness_failure_category"] = "timeout"
                    if state["status"] == "completed":
                        report_response = await client.get(
                            f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}/report"
                        )
                        result["report_api_http_status"] = report_response.status_code
                        if report_response.status_code == 200 and save_report:
                            _write_private(output_dir / f"run-{run_id}.md", report_response.json()["body"])
                    else:
                        result["report_api_http_status"] = None
                if service._engine is not None and task_id is not None and run_id is not None:
                    result["database"] = await collect_snapshot(build_session_factory(service._engine), task_id, run_id)
            finally:
                await service.stop()
    except Exception as exc:
        result["harness_failure_type"] = type(exc).__name__
        result["harness_failure_category"] = classify_failure(str(exc))
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        result["task_id"] = str(task_id) if task_id is not None else None
        result["run_id"] = str(run_id) if run_id is not None else None
        result["outbound"] = gate.summary()
        name = f"run-{run_id}" if run_id else f"pre-run-{uuid4().hex}"
        output = output_dir / f"{name}.json"
        _write_private(output, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return output, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="3I-2: bounded REAL provider E2E acceptance. Default is offline preflight."
    )
    parser.add_argument("--execute", action="store_true", help="Start one real API-billed research task")
    parser.add_argument("--allow-billing", action="store_true", help="Explicitly acknowledge live provider charges")
    parser.add_argument("--query", type=str, help="One bounded, factual research question")
    parser.add_argument("--output-dir", type=Path, help="Absolute output directory OUTSIDE the Git checkout")
    parser.add_argument(
        "--save-report", action="store_true", help="Save full final report locally (contains web content)"
    )
    parser.add_argument("--max-requests", type=int, default=16)
    parser.add_argument("--max-model-requests", type=int, default=10)
    parser.add_argument("--max-tavily-requests", type=int, default=6)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-model-request-bytes", type=int, default=32_000)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        budget = LiveBudget(
            args.max_requests,
            args.max_model_requests,
            args.max_tavily_requests,
            args.max_output_tokens,
            args.timeout_seconds,
            max_model_request_bytes=args.max_model_request_bytes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    from enterprise_research.acceptance.financial_workload import financial_workload_issues

    issues = [*preflight(dict(os.environ)), *financial_workload_issues(os.environ)]
    if not args.execute:
        print("3I-2 OFFLINE preflight: no provider or database connection is made.")
        for issue in issues:
            print(" -", issue)
        if not issues:
            print("Local configuration checks passed. No charges incurred by preflight.")
        return 1 if issues else 0
    if issues:
        print("Preflight failed. No outbound provider calls made.")
        for issue in issues:
            print(" -", issue)
        return 2
    if not (args.allow_billing and os.getenv("ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE") == "1"):
        print("Live calls blocked: require --execute --allow-billing and ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE=1")
        return 2
    if not args.query or not args.query.strip() or len(args.query) > 4000:
        print("Live execution requires one nonempty --query of at most 4000 characters")
        return 2
    if args.output_dir is None or not args.output_dir.is_absolute():
        print("Live execution requires an absolute --output-dir outside the Git workspace")
        return 2
    out = args.output_dir.resolve()
    if out.is_relative_to(Path.cwd().resolve()):
        print("Live result directory cannot be inside the working Git project")
        return 2
    path, report = asyncio.run(execute_live(args.query, out, budget, save_report=args.save_report))
    print(f"Live acceptance: {report['status']}; requests={report['outbound']['requests_started']}; result={path}")
    return (
        0
        if (
            report.get("status") == "completed"
            and report.get("report_api_http_status") == 200
            and "database" in report
            and report.get("sse_terminal_observed") is True
            and "harness_failure_type" not in report
            and report["database"].get("status") == "completed"
            and report["database"]["artifacts"].get("verification_receipt_present") is True
            and report["database"]["artifacts"].get("synthesis_receipt_present") is True
            and report["database"]["artifacts"].get("mechanically_verified_citation_ratio") == 1.0
            and report["database"]["artifacts"].get("report_sha256_valid") is True
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
