"""OpenAI-compatible claim adapter tests; HTTP only, no live model calls."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from enterprise_research.application.claim_generation import (
    ClaimGenerationContext,
    ClaimGenerationLimits,
    ClaimInputEvidence,
)
from enterprise_research.infrastructure.claim_generation import (
    ClaimGeneratorAdapterError,
    OpenAICompatibleClaimGenerator,
    OpenAICompatibleClaimGeneratorSettings,
)


def context():
    return ClaimGenerationContext(
        task_id=uuid4(),
        run_id=uuid4(),
        fencing_token=3,
        task_query="Evidence-based comparison",
        objective="Build candidate findings",
        questions=("What does the source say?",),
        evidence=(ClaimInputEvidence(uuid4(), uuid4(), "Potentially relevant evidence.", "chars:0-30", "c" * 64),),
        available_evidence_count=1,
        input_sha256="d" * 64,
    )


def settings(*, max_candidates=2):
    return OpenAICompatibleClaimGeneratorSettings(
        base_url="https://example.test/v1",
        api_key="example-secret",
        model="mock-glm",
        limits=ClaimGenerationLimits(max_candidates=max_candidates),
    )


def test_adapter_proposes_structured_claims_without_claim_status():
    async def exercise():
        ctx = context()

        async def handle(request):
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer example-secret"
            payload = json.loads(request.content)
            assert "SUPPORTED" not in payload["messages"][1]["content"]
            assert str(ctx.evidence[0].evidence_id) in payload["messages"][1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "claims": [
                                            {"text": "One proposal", "evidence_ids": [str(ctx.evidence[0].evidence_id)]}
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await OpenAICompatibleClaimGenerator(settings(), client=client).propose(ctx)
        assert len(result) == 1 and result[0].evidence_ids == (ctx.evidence[0].evidence_id,)
        assert not hasattr(result[0], "status")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "content, match",
    [
        ("```json\n{}\n```", "malformed"),
        (json.dumps({"claims": [{"text": "x", "evidence_ids": ["not-a-uuid"]}]}), "malformed"),
        (json.dumps({"claims": [{"text": "x", "evidence_ids": []}]}), "malformed"),
        (json.dumps({"claims": [{"text": "x", "evidence_ids": [str(uuid4())], "status": "supported"}]}), "malformed"),
        (json.dumps({"claims": [{"text": "x", "evidence_ids": [str(uuid4())]}] * 3}), "budget"),
    ],
)
def test_adapter_fails_closed_on_malformed_or_excess_json(content, match):
    async def exercise():
        async def handle(_request):
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ClaimGeneratorAdapterError, match=match):
                await OpenAICompatibleClaimGenerator(settings(), client=client).propose(context())

    asyncio.run(exercise())


def test_adapter_fails_closed_on_http_error():
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with pytest.raises(ClaimGeneratorAdapterError, match="request failed"):
                await OpenAICompatibleClaimGenerator(settings(), client=client).propose(context())

    asyncio.run(exercise())
