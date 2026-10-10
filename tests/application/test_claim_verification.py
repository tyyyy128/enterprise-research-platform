"""3G: semantic decision scope, exact-source quotes and stage ordering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from enterprise_research.application.claim_generation import ClaimInputEvidence
from enterprise_research.application.claim_verification import (
    ClaimVerificationLimits,
    ClaimVerificationReceipt,
    ClaimVerificationVerdict,
    EvidenceQuote,
    ResearchClaimVerificationService,
    VerificationClaim,
    VerificationContext,
    validate_verdicts,
    verdicts_digest,
    verification_receipt_digest,
)
from enterprise_research.application.errors import ResearchClaimVerificationError
from enterprise_research.application.pipeline import ResearchPipelineStageRunner
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.domain import ClaimStatus, ResearchRunStatus


def context(*, empty=False):
    task_id, run_id = uuid4(), uuid4()
    evidence_id, other_id = uuid4(), uuid4()
    claims = () if empty else (VerificationClaim(uuid4(), "Revenue grew 14% in 2025.", (evidence_id,)),)
    evidence = (
        ClaimInputEvidence(evidence_id, uuid4(), "Revenue grew 14% in 2025.", None, "a" * 64),
        ClaimInputEvidence(other_id, uuid4(), "Revenue fell 3% in 2025.", None, "b" * 64),
    )
    ctx = VerificationContext(task_id, run_id, 11, "c" * 64, "d" * 64, evidence, claims, "e" * 64)
    lease = ResearchRunLease(run_id, uuid4(), 11, datetime.now(UTC) + timedelta(seconds=120))
    return ctx, lease


def supported(ctx):
    return ClaimVerificationVerdict(
        ctx.claims[0].claim_id,
        ClaimStatus.SUPPORTED,
        0.95,
        "The first source explicitly reports the growth rate.",
        (EvidenceQuote(ctx.evidence[0].evidence_id, "Revenue grew 14%"),),
    )


def test_supported_decision_requires_exact_quote_and_independent_claim_id():
    ctx, _ = context()
    results = validate_verdicts(ctx, [supported(ctx)], ClaimVerificationLimits())
    assert results[0].status == ClaimStatus.SUPPORTED
    assert len(verdicts_digest(results)) == 64
    assert verdicts_digest(results) != verdicts_digest(())
    assert verification_receipt_digest(results, model="independent-a", limits=ClaimVerificationLimits()) != (
        verification_receipt_digest(results, model="independent-b", limits=ClaimVerificationLimits())
    )


@pytest.mark.parametrize(
    "mutation,pattern",
    [
        (
            lambda v, c: [ClaimVerificationVerdict(uuid4(), v.status, v.confidence, v.rationale, v.supporting)],
            "unknown",
        ),
        (lambda v, c: [v, v], "exactly once"),
        (lambda v, c: [], "exactly once"),
        (
            lambda v, c: [ClaimVerificationVerdict(v.claim_id, v.status, 0.1, v.rationale, v.supporting)],
            "high confidence",
        ),
        (
            lambda v, c: [ClaimVerificationVerdict(v.claim_id, v.status, float("nan"), v.rationale, v.supporting)],
            "finite",
        ),
        (
            lambda v, c: [
                ClaimVerificationVerdict(
                    v.claim_id,
                    v.status,
                    0.9,
                    v.rationale,
                    (EvidenceQuote(c.evidence[0].evidence_id, "fabricated quotation"),),
                )
            ],
            "exact",
        ),
        (
            lambda v, c: [
                ClaimVerificationVerdict(
                    v.claim_id, v.status, 0.9, v.rationale, (EvidenceQuote(c.evidence[1].evidence_id, "Revenue fell"),)
                )
            ],
            "exact",
        ),
        (
            lambda v, c: [
                ClaimVerificationVerdict(
                    v.claim_id,
                    v.status,
                    0.9,
                    v.rationale,
                    v.supporting,
                    (EvidenceQuote(c.evidence[1].evidence_id, "Revenue fell"),),
                )
            ],
            "no conflicts",
        ),
        (lambda v, c: [ClaimVerificationVerdict(v.claim_id, v.status, 0.9, "   ", v.supporting)], "rationale"),
    ],
)
def test_untrusted_verdicts_never_pass_structural_acceptance(mutation, pattern):
    ctx, _ = context()
    with pytest.raises(ResearchClaimVerificationError, match=pattern):
        validate_verdicts(ctx, mutation(supported(ctx), ctx), ClaimVerificationLimits())


def test_conflicted_requires_two_distinct_real_quoted_passages():
    ctx, _ = context()
    verdict = ClaimVerificationVerdict(
        ctx.claims[0].claim_id,
        ClaimStatus.CONFLICTED,
        0.91,
        "The two excerpts disagree on direction.",
        (EvidenceQuote(ctx.evidence[0].evidence_id, "Revenue grew 14%"),),
        (EvidenceQuote(ctx.evidence[1].evidence_id, "Revenue fell 3%"),),
    )
    assert validate_verdicts(ctx, [verdict], ClaimVerificationLimits())[0].status == ClaimStatus.CONFLICTED
    with pytest.raises(ResearchClaimVerificationError, match="distinct"):
        validate_verdicts(
            ctx,
            [
                ClaimVerificationVerdict(
                    verdict.claim_id, verdict.status, 0.91, verdict.rationale, verdict.supporting, ()
                )
            ],
            ClaimVerificationLimits(),
        )


def test_unsupported_may_explicitly_report_insufficient_evidence():
    ctx, _ = context()
    verdict = ClaimVerificationVerdict(ctx.claims[0].claim_id, ClaimStatus.UNSUPPORTED, 0.8, "Not conclusive.")
    assert validate_verdicts(ctx, [verdict], ClaimVerificationLimits())[0].status == ClaimStatus.UNSUPPORTED


class Repository:
    def __init__(self, ctx):
        self.ctx, self.writes = ctx, []

    async def load_context(self, task_id, run_id, lease, limits):
        assert (task_id, run_id, lease.fencing_token) == (self.ctx.task_id, self.ctx.run_id, 11)
        return self.ctx

    async def save_guarded(self, ctx, lease, limits, verdicts, *, model):
        self.writes.append((ctx, verdicts, model))
        return ClaimVerificationReceipt(
            ctx.task_id,
            ctx.run_id,
            11,
            ctx.input_sha256,
            verdicts_digest(verdicts),
            tuple(item.claim_id for item in verdicts),
            model,
        )


class Model:
    def __init__(self, results):
        self.results, self.calls = results, 0

    async def assess(self, ctx):
        self.calls += 1
        return self.results


def test_service_persists_only_validated_verdicts():
    async def exercise():
        ctx, lease = context()
        repo = Repository(ctx)
        service = ResearchClaimVerificationService(repo, Model([supported(ctx)]), model="mock-verifier")
        receipt = await service.verify_run(ctx.task_id, ctx.run_id, lease=lease)
        assert receipt.claim_ids == (ctx.claims[0].claim_id,)
        assert repo.writes[0][1][0].status == ClaimStatus.SUPPORTED

    asyncio.run(exercise())


def test_zero_candidates_skips_model_but_emits_verifier_receipt():
    async def exercise():
        ctx, lease = context(empty=True)
        repo = Repository(ctx)
        model = Model([supported(context()[0])])
        result = await ResearchClaimVerificationService(repo, model, model="mock").verify_run(
            ctx.task_id, ctx.run_id, lease=lease
        )
        assert result.claim_ids == () and model.calls == 0
        assert repo.writes[0][1] == ()

    asyncio.run(exercise())


def test_no_lease_and_model_invalid_result_never_write():
    async def exercise():
        ctx, lease = context()
        repo = Repository(ctx)
        service = ResearchClaimVerificationService(repo, Model([]), model="mock")
        with pytest.raises(ResearchClaimVerificationError, match="fenced lease"):
            await service.verify_run(ctx.task_id, ctx.run_id, lease=None)
        with pytest.raises(ResearchClaimVerificationError, match="exactly once"):
            await service.verify_run(ctx.task_id, ctx.run_id, lease=lease)
        assert not repo.writes

    asyncio.run(exercise())


def test_pipeline_runs_generation_then_verification_before_writing():
    async def exercise():
        ctx, lease = context()
        calls = []

        class EvidenceService:
            async def extract_run(self, *args, **kwargs):
                calls.append("evidence")

        class Generator:
            async def generate_run(self, *args, **kwargs):
                calls.append("propose")

        class Verifier:
            async def verify_run(self, *args, **kwargs):
                calls.append("verify")
                assert kwargs["lease"] == lease

        runner = ResearchPipelineStageRunner(
            object(),  # type: ignore[arg-type]
            evidence_extraction_service=EvidenceService(),  # type: ignore[arg-type]
            claim_generation_service=Generator(),  # type: ignore[arg-type]
            claim_verification_service=Verifier(),  # type: ignore[arg-type]
        )
        await runner.run_stage(ctx.task_id, ctx.run_id, ResearchRunStatus.VERIFYING, lease=lease)
        assert calls == ["evidence", "propose", "verify"]

    asyncio.run(exercise())
