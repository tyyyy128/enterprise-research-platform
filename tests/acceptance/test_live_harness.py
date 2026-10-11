"""Offline-only safety and audit regression; never touches live providers or PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from enterprise_research.acceptance.live_harness import (
    LiveAcceptanceError,
    LiveBudget,
    OutboundBudgetGate,
    RequestBudgetExceeded,
    _apply_output_token_cap,
    audit_snapshot,
    build_parser,
    classify_failure,
    endpoint_inventory,
    main,
    preflight,
    temporary_workload_env,
)


def complete_env():
    environ = {
        "ENTERPRISE_RESEARCH_DB_NAME": "enterprise_research",
        "ENTERPRISE_RESEARCH_DB_HOST": "127.0.0.1",
        "ENTERPRISE_RESEARCH_DB_USER": "dev",
        "ENTERPRISE_RESEARCH_DB_PASSWORD": "NEVER_LOG_DB_PASSWORD",
        "TAVILY_API_KEY": "NEVER_LOG_TAVILY_KEY",
    }
    for stage in ("PLANNER", "EVIDENCE", "CLAIM", "VERIFY", "SYNTHESIS"):
        prefix = f"ENTERPRISE_RESEARCH_{stage}"
        environ[f"{prefix}_BASE_URL"] = "https://model.example.invalid/v4"
        environ[f"{prefix}_API_KEY"] = f"NEVER_LOG_{stage}_KEY"
        environ[f"{prefix}_MODEL"] = "glm-test"
    return environ


def test_default_is_offline_and_opt_in_is_explicit():
    assert build_parser().parse_args([]).execute is False
    assert build_parser().parse_args(["--execute"]).allow_billing is False


def test_preflight_never_prints_credential_values():
    environ = complete_env()
    assert preflight(environ) == []
    environ["ENTERPRISE_RESEARCH_DB_HOST"] = "postgres.production.example"
    problems = preflight(environ)
    assert any("DB host" in problem for problem in problems)
    assert "NEVER_LOG" not in str(problems)
    environ["ENTERPRISE_RESEARCH_DB_HOST"] = "127.0.0.1"
    environ["ENTERPRISE_RESEARCH_PLANNER_API_KEY"] = ""
    assert "Missing ENTERPRISE_RESEARCH_PLANNER_API_KEY" in preflight(environ)


def test_reject_http_model_endpoint_and_tavily_override():
    environ = complete_env()
    environ["ENTERPRISE_RESEARCH_PLANNER_BASE_URL"] = "http://model.invalid/v4"
    assert any("HTTPS" in problem for problem in preflight(environ))
    environ = complete_env()
    environ["ENTERPRISE_RESEARCH_RETRIEVAL_BASE_URL"] = "https://not-tavily.invalid"
    assert "official Tavily" in preflight(environ)[0]


def test_endpoint_inventory_only_maps_exact_urls():
    endpoints = endpoint_inventory(complete_env())
    assert endpoints["https://api.tavily.com/search"] == "tavily_search"
    assert endpoints["https://api.tavily.com/extract"] == "tavily_extract"
    assert endpoints["https://model.example.invalid/v4/chat/completions"] == "model"
    assert "https://model.example.invalid/v4/chat/completions?key=secret" not in endpoints


def test_budget_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="max_model_requests"):
        LiveBudget(max_requests=3, max_model_requests=4)
    with pytest.raises(ValueError, match="max_output_tokens"):
        LiveBudget(max_output_tokens_per_request=10)
    with pytest.raises(ValueError, match="timeout"):
        LiveBudget(timeout_seconds=10000)
    with pytest.raises(ValueError, match="max_model_request_bytes"):
        LiveBudget(max_model_request_bytes=64)


def test_per_request_output_cap_preserves_request_payload_and_headers():
    request = httpx.Request(
        "POST",
        "https://model.example.invalid/v4/chat/completions",
        headers={"Authorization": "Bearer NEVER_LOG_SECRET"},
        json={"model": "glm-test", "messages": [{"role": "user", "content": "test"}], "max_tokens": 7000},
    )
    bounded = _apply_output_token_cap(request, 1024)
    assert json.loads(bounded.content)["max_tokens"] == 1024
    assert json.loads(request.content)["max_tokens"] == 7000
    assert bounded.headers["Authorization"] == "Bearer NEVER_LOG_SECRET"


def test_token_cap_recomputes_content_length_after_rewriting_json():
    """A real HTTP/1.1 transport must receive the modified body length."""
    original = httpx.Request(
        "POST",
        "https://model.example.invalid/v4/chat/completions",
        headers={"Authorization": "Bearer NEVER_LOG_SECRET"},
        json={"model": "glm-test", "messages": [{"role": "user", "content": "审计"}]},
    )
    bounded = _apply_output_token_cap(original, 512)
    assert int(original.headers["content-length"]) == len(original.content)
    assert len(bounded.content) > len(original.content)
    assert int(bounded.headers["content-length"]) == len(bounded.content)
    assert bounded.headers["Authorization"] == original.headers["Authorization"]
    assert json.loads(bounded.content)["max_tokens"] == 512


def test_gate_counts_real_external_sends_and_never_logs_inputs():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 13, "completion_tokens": 8}})

    async def exercise():
        gate = OutboundBudgetGate(
            LiveBudget(max_requests=2, max_model_requests=1, max_tavily_requests=1),
            endpoint_inventory(complete_env()),
        )
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                response = await client.post(
                    "https://model.example.invalid/v4/chat/completions",
                    headers={"Authorization": "Bearer NEVER_LOG_SECRET"},
                    json={"model": "glm-test", "messages": [], "max_tokens": 7000},
                )
                assert response.status_code == 200
                with pytest.raises(RequestBudgetExceeded):
                    await client.post(
                        "https://model.example.invalid/v4/chat/completions",
                        json={"model": "glm-test", "messages": []},
                    )
                await client.post("https://api.tavily.com/search", json={"query": "test"})
                with pytest.raises(RequestBudgetExceeded):
                    await client.post("https://api.tavily.com/extract", json={"urls": "test"})
        assert len(seen) == 2
        assert json.loads(seen[0].content)["max_tokens"] == 2048
        result = gate.summary()
        assert result["requests_started"] == 2
        assert result["requests_blocked"] == 2
        assert result["prompt_tokens_observed"] == 13
        assert result["completion_tokens_observed"] == 8
        assert "NEVER_LOG_SECRET" not in json.dumps(result)
        assert "messages" not in json.dumps(result)

    asyncio.run(exercise())


def test_gate_rejects_unapproved_destination_before_network():
    network_calls = []

    def respond(request):
        network_calls.append(request)
        return httpx.Response(200, json={})

    async def exercise():
        gate = OutboundBudgetGate(LiveBudget(), endpoint_inventory(complete_env()))
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(LiveAcceptanceError, match="unapproved"):
                    await client.get("https://api.tavily.com/search")
                with pytest.raises(LiveAcceptanceError, match="unapproved"):
                    await client.post("https://unknown.invalid/other", json={})
                with pytest.raises(LiveAcceptanceError, match="unapproved"):
                    await client.post("https://model.example.invalid/v4/chat/completions?token=SECRET", json={})
        assert network_calls == []
        assert gate.summary()["requests_started"] == 0

    asyncio.run(exercise())


def test_gate_ignores_only_actual_asgi_transport_requests():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/local")
    async def local():
        return {"ok": True}

    async def exercise():
        gate = OutboundBudgetGate(LiveBudget(), endpoint_inventory(complete_env()))
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                response = await client.get("/local")
                assert response.json() == {"ok": True}
        assert gate.summary()["requests_started"] == 0

    asyncio.run(exercise())


def test_workload_environment_restored_even_after_exception():
    env = {"ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS": "9", "KEEP_ME": "yes"}
    with pytest.raises(RuntimeError):
        with temporary_workload_env(env):
            assert env["ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS"] == "1"
            assert env["ENTERPRISE_RESEARCH_WRITER_BACKEND"] == "openai-compatible"
            raise RuntimeError("intentional")
    assert env == {"ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS": "9", "KEEP_ME": "yes"}


def test_failure_classification_does_not_return_source_error_text():
    assert classify_failure("Evidence mismatch Authorization Bearer SECRET") == "evidence"
    assert classify_failure("model request timed out") == "timeout"
    assert classify_failure(None) is None


def test_audit_uses_only_current_run_provenance_and_is_not_semantic_score():
    run_id, other_run, task_id, source_id, question_id, evidence_id, claim_id = (uuid4() for _ in range(7))
    now = datetime.now(UTC)
    source = SimpleNamespace(
        id=source_id,
        url="https://filing.example/2025",
        source_metadata={
            "search": {
                "hits": [
                    {"run_id": str(other_run), "question_id": str(uuid4())},
                    {"run_id": str(run_id), "question_id": str(question_id)},
                ]
            }
        },
    )
    report_body = "# a\nOne fact.\n"
    excerpt = "Original exact excerpt"
    payload = json.dumps([str(source_id), "document:0", excerpt], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    audit = audit_snapshot(
        task_id=task_id,
        run_id=run_id,
        run=SimpleNamespace(status="completed", error=None),
        plan=object(),
        questions=[SimpleNamespace(id=question_id)],
        sources=[source],
        documents=[SimpleNamespace(source_id=source_id, status="succeeded")],
        manifest=SimpleNamespace(
            evidence_ids=[str(evidence_id)],
            evidence_hashes={str(evidence_id): fingerprint},
        ),
        generation=SimpleNamespace(claim_ids=[str(claim_id)]),
        verification=object(),
        claims=[SimpleNamespace(id=claim_id, status="supported")],
        evidence_rows=[
            SimpleNamespace(
                id=evidence_id,
                source_id=source_id,
                locator="document:0",
                content=excerpt,
            )
        ],
        claim_links=[SimpleNamespace(claim_id=claim_id, evidence_id=evidence_id)],
        report=SimpleNamespace(
            body=report_body,
            body_sha256=hashlib.sha256(report_body.encode()).hexdigest(),
            citations=[
                {
                    "claim_id": str(claim_id),
                    "evidence": [
                        {
                            "evidence_id": str(evidence_id),
                            "source_id": str(source_id),
                            "url": source.url,
                            "fingerprint": fingerprint,
                        }
                    ],
                }
            ],
        ),
        synthesis=object(),
        journal=[
            SimpleNamespace(
                stage="writing",
                outcome="completed",
                started_at=now,
                finished_at=now + timedelta(seconds=3),
                fencing_token=1,
            )
        ],
    )
    assert audit["artifacts"]["question_search_coverage"] == 1.0
    assert audit["artifacts"]["mechanically_verified_citation_ratio"] == 1.0
    assert audit["artifacts"]["verified_supported"] == 1
    assert audit["stage_journal"][0]["duration_seconds"] == 3
    assert len(audit["review_required"]) >= 2
    assert "semantic_accuracy" not in audit


def test_live_execution_requires_three_independent_flags(monkeypatch, tmp_path, capsys):
    for key, value in complete_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE", raising=False)
    result = main(["--execute", "--query", "test", "--output-dir", str(tmp_path)])
    assert result == 2
    assert list(tmp_path.iterdir()) == []
    assert "blocked" in capsys.readouterr().out
    monkeypatch.setenv("ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE", "1")
    result = main(["--execute", "--query", "test", "--output-dir", str(tmp_path)])
    assert result == 2
    assert list(tmp_path.iterdir()) == []


def test_gate_concurrency_does_not_overshoot_external_budget():
    external_calls = []

    async def respond(request):
        external_calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"results": []})

    async def exercise():
        gate = OutboundBudgetGate(
            LiveBudget(max_requests=2, max_model_requests=1, max_tavily_requests=2),
            endpoint_inventory(complete_env()),
        )
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                responses = await asyncio.gather(
                    *(client.post("https://api.tavily.com/search", json={"query": "safe"}) for _ in range(8)),
                    return_exceptions=True,
                )
        assert sum(isinstance(result, httpx.Response) for result in responses) == 2
        assert sum(isinstance(result, RequestBudgetExceeded) for result in responses) == 6
        assert len(external_calls) == 2
        assert gate.summary()["requests_blocked"] == 6

    asyncio.run(exercise())


def test_gate_records_http_failure_without_raw_provider_message():
    async def respond(_request):
        raise httpx.ConnectError("Bearer SHOULD_NOT_BE_LOGGED")

    async def exercise():
        gate = OutboundBudgetGate(LiveBudget(), endpoint_inventory(complete_env()))
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.post("https://api.tavily.com/search", json={"query": "test"})
        result = json.dumps(gate.summary())
        assert "SHOULD_NOT_BE_LOGGED" not in result
        assert '"failure_type": "ConnectError"' in result

    asyncio.run(exercise())


def test_model_input_size_budget_blocks_before_network():
    actual_requests = []

    def respond(request):
        actual_requests.append(request)
        return httpx.Response(200, json={})

    async def exercise():
        gate = OutboundBudgetGate(LiveBudget(max_model_request_bytes=1024), endpoint_inventory(complete_env()))
        with gate.install():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(RequestBudgetExceeded, match="input-size"):
                    await client.post(
                        "https://model.example.invalid/v4/chat/completions",
                        json={"model": "glm-test", "messages": [{"content": "x" * 2000}]},
                    )
        assert actual_requests == []
        assert gate.summary()["requests_started"] == 0
        assert gate.summary()["requests_blocked"] == 1

    asyncio.run(exercise())
