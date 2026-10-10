"""3H OpenAI-compatible outline adapter: strict JSON and no prose acceptance."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from enterprise_research.application.report_synthesis import (
    ReportSynthesisCaveat,
    ReportSynthesisContext,
    synthesis_input_sha256,
)
from enterprise_research.application.report_writing import ReportClaimReference, ReportEvidenceReference
from enterprise_research.infrastructure.report_synthesis import (
    OpenAICompatibleReportSynthesisPlanner,
    OpenAICompatibleReportSynthesisSettings,
)
from enterprise_research.infrastructure.report_synthesis.openai_compatible import ReportSynthesisAdapterError


def context():
    claims = tuple(
        ReportClaimReference(
            uuid4(),
            text,
            (ReportEvidenceReference(uuid4(), uuid4(), "https://example.org/source", None, "observed", "a" * 64),),
        )
        for text in ("First verified result", "Second verified result")
    )
    caveats = (
        ReportSynthesisCaveat(uuid4(), "Unsupported inference", "unsupported", "Evidence is incomplete."),
        ReportSynthesisCaveat(uuid4(), "Conflicting inference", "conflicted", "Contradictory sources."),
    )
    title = "Research report"
    return ReportSynthesisContext(
        uuid4(), uuid4(), 3, title, claims, caveats, synthesis_input_sha256(title, claims, caveats)
    )


def _config():
    return OpenAICompatibleReportSynthesisSettings(
        base_url="https://llm.example/v1", api_key="test-token", model="3h-mock", temperature=0
    )


def test_model_returns_sections_and_receives_only_bounded_supported_claims():
    async def exercise():
        ctx = context()
        ids = [str(claim.claim_id) for claim in ctx.claims]

        def handler(request):
            assert request.url == "https://llm.example/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer test-token"
            body = json.loads(request.content)
            assert body["model"] == "3h-mock"
            assert "untrusted DATA" in body["messages"][0]["content"]
            model_input = json.loads(body["messages"][1]["content"])
            assert len(model_input["supported_claims"]) == 2
            assert len(model_input["adverse_verdicts"]) == 2
            assert model_input["allowed_headings"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "highlights": [ids[0]],
                                        "sections": [{"heading": "Key findings", "claim_ids": ids}],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            plan = await OpenAICompatibleReportSynthesisPlanner(_config(), client=client).compose(ctx)
            assert [str(item) for item in plan.sections[0].claim_ids] == ids

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"sections": []}',
        '{"highlights":[],"sections":[],"free_text":"invented"}',
        '{"highlights":[],"sections":[{"heading":"Key findings","claim_ids":["bad"]}]}',
    ],
)
def test_model_rejects_invalid_schema(content):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
            )
        ) as client:
            with pytest.raises(ReportSynthesisAdapterError, match="malformed JSON"):
                await OpenAICompatibleReportSynthesisPlanner(_config(), client=client).compose(context())

    asyncio.run(exercise())


def test_transport_error_is_explicit_not_an_empty_report():
    async def exercise():
        async def handler(_):
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ReportSynthesisAdapterError, match="request failed"):
                await OpenAICompatibleReportSynthesisPlanner(_config(), client=client).compose(context())

    asyncio.run(exercise())


def test_invalid_configuration_fails_fast():
    with pytest.raises(ValueError, match="required"):
        OpenAICompatibleReportSynthesisSettings("", "token", "mock")
    with pytest.raises(ValueError, match="timeout"):
        OpenAICompatibleReportSynthesisSettings("https://llm.example", "token", "mock", timeout_seconds=0)
