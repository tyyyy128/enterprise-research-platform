"""3G verifier adapter JSON-contract tests without external network calls."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from enterprise_research.application.claim_generation import ClaimInputEvidence
from enterprise_research.application.claim_verification import VerificationClaim, VerificationContext
from enterprise_research.domain import ClaimStatus
from enterprise_research.infrastructure.claim_verification import (
    ClaimVerifierAdapterError,
    OpenAICompatibleClaimVerifier,
    OpenAICompatibleClaimVerifierSettings,
)


def context():
    e_id = uuid4()
    claim = VerificationClaim(uuid4(), "Sales grew 14%.", (e_id,))
    evidence = ClaimInputEvidence(e_id, uuid4(), "Sales grew 14%.", None, "a" * 64)
    return VerificationContext(uuid4(), uuid4(), 1, "b" * 64, "c" * 64, (evidence,), (claim,), "d" * 64)


def settings():
    return OpenAICompatibleClaimVerifierSettings("https://example.test/v1", "token", "verify-model")


def test_verifier_model_receives_raw_excerpts_and_returns_judgments():
    async def exercise():
        ctx = context()

        def handle(req):
            body = json.loads(req.content)
            assert req.url.path == "/v1/chat/completions"
            assert req.headers["authorization"] == "Bearer token"
            assert str(ctx.claims[0].claim_id) in body["messages"][1]["content"]
            assert "Sales grew 14%." in body["messages"][1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "decisions": [
                                            {
                                                "claim_id": str(ctx.claims[0].claim_id),
                                                "status": "supported",
                                                "confidence": 0.95,
                                                "rationale": "Direct passage",
                                                "supporting": [
                                                    {
                                                        "evidence_id": str(ctx.evidence[0].evidence_id),
                                                        "quote": "Sales grew 14%",
                                                    }
                                                ],
                                                "contradicting": [],
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            results = await OpenAICompatibleClaimVerifier(settings(), client=client).assess(ctx)
        assert results[0].status == ClaimStatus.SUPPORTED
        assert results[0].supporting[0].quote == "Sales grew 14%"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{}\n```",
        "{}",
        '{"decisions":[{"status":"fake"}]}',
        '{"decisions":[{"claim_id":"nope", "status":"supported"}]}',
        (
            '{"decisions":[{"claim_id":"nope", "status":"supported", "confidence": 0.5, '
            '"rationale": "a", "supporting": [], "contradicting": [], "extra": true}]}'
        ),
    ],
)
def test_bad_provider_payload_fails_closed(content):
    async def exercise():
        async def handle(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ClaimVerifierAdapterError, match="malformed"):
                await OpenAICompatibleClaimVerifier(settings(), client=client).assess(context())

    asyncio.run(exercise())
