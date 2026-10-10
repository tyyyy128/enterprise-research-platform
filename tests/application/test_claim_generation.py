"""Provider-neutral 3F candidate scope, budget and pipeline dispatch tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from enterprise_research.application.claim_generation import (
    ClaimCandidate,
    ClaimGenerationContext,
    ClaimGenerationLimits,
    ClaimGenerationReceipt,
    ClaimInputEvidence,
    ResearchClaimGenerationService,
    candidate_id,
    candidate_output_digest,
)
from enterprise_research.application.errors import ResearchClaimGenerationError
from enterprise_research.application.pipeline import ResearchPipelineStageRunner
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.domain import ResearchRunStatus


def fixture_context(*, empty: bool = False):
    task_id, run_id, evidence_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    evidence = () if empty else (ClaimInputEvidence(evidence_id, source_id, "Exact quote.", "chars:0-12", "a" * 64),)
    context = ClaimGenerationContext(
        task_id=task_id,
        run_id=run_id,
        fencing_token=7,
        task_query="Research question",
        objective="Propose possible findings",
        questions=("What happened?",),
        evidence=evidence,
        available_evidence_count=len(evidence),
        input_sha256="b" * 64,
    )
    lease = ResearchRunLease(
        run_id=run_id,
        owner_id=uuid4(),
        fencing_token=7,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    return context, lease


class RecordingRepository:
    def __init__(self, context):
        self.context = context
        self.saved = []

    async def load_context(self, task_id, run_id, lease, limits):
        assert (task_id, run_id, lease.fencing_token) == (self.context.task_id, self.context.run_id, 7)
        return self.context

    async def save_guarded(self, context, lease, limits, candidates, *, model):
        self.saved.append((context, lease, limits, candidates, model))
        return ClaimGenerationReceipt(
            task_id=context.task_id,
            run_id=context.run_id,
            fencing_token=lease.fencing_token,
            input_sha256=context.input_sha256,
            output_sha256=candidate_output_digest(context.run_id, candidates),
            available_evidence_count=context.available_evidence_count,
            selected_evidence_ids=tuple(item.evidence_id for item in context.evidence),
            claim_ids=tuple(candidate_id(context.run_id, candidate) for candidate in candidates),
            generator_model=model,
        )


class FixedGenerator:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    async def propose(self, context):
        self.calls += 1
        return self.candidates


def test_generates_only_candidates_with_explicit_scope_and_stable_run_ids():
    async def exercise():
        ctx, lease = fixture_context()
        repo = RecordingRepository(ctx)
        generator = FixedGenerator([ClaimCandidate("  Claim   from   excerpt. ", (ctx.evidence[0].evidence_id,))])
        receipt = await ResearchClaimGenerationService(repo, generator, model="mock-model").generate_run(
            ctx.task_id, ctx.run_id, lease=lease
        )
        assert generator.calls == 1
        assert len(receipt.claim_ids) == 1
        assert receipt.claim_ids[0] == candidate_id(
            ctx.run_id, ClaimCandidate("Claim from excerpt.", (ctx.evidence[0].evidence_id,))
        )
        assert repo.saved[0][3][0].text == "Claim from excerpt."
        assert candidate_id(uuid4(), repo.saved[0][3][0]) != receipt.claim_ids[0]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "candidates, error",
    [
        ([ClaimCandidate("fabricated", (uuid4(),))], "out-of-scope"),
        ([ClaimCandidate("no references", ())], "out-of-scope"),
        ([ClaimCandidate("   ", (uuid4(),))], "blank"),
        ([ClaimCandidate("a", (uuid4(),)), ClaimCandidate("b", (uuid4(),))], "count"),
    ],
)
def test_invalid_or_excess_proposals_never_call_persistence(candidates, error):
    async def exercise():
        ctx, lease = fixture_context()
        repo = RecordingRepository(ctx)
        service = ResearchClaimGenerationService(
            repo, FixedGenerator(candidates), model="model", limits=ClaimGenerationLimits(max_candidates=1)
        )
        with pytest.raises(ResearchClaimGenerationError, match=error):
            await service.generate_run(ctx.task_id, ctx.run_id, lease=lease)
        assert repo.saved == []

    asyncio.run(exercise())


def test_duplicate_candidates_are_rejected_before_save():
    async def exercise():
        ctx, lease = fixture_context()
        evidence_id = ctx.evidence[0].evidence_id
        repo = RecordingRepository(ctx)
        service = ResearchClaimGenerationService(
            repo,
            FixedGenerator([ClaimCandidate("Same", (evidence_id,)), ClaimCandidate(" same ", (evidence_id,))]),
            model="mock-model",
        )
        with pytest.raises(ResearchClaimGenerationError, match="duplicate"):
            await service.generate_run(ctx.task_id, ctx.run_id, lease=lease)
        assert not repo.saved

    asyncio.run(exercise())


def test_zero_evidence_persists_explicit_empty_receipt_without_model_call():
    async def exercise():
        ctx, lease = fixture_context(empty=True)
        repo = RecordingRepository(ctx)
        generator = FixedGenerator([ClaimCandidate("cannot exist", (uuid4(),))])
        receipt = await ResearchClaimGenerationService(repo, generator, model="mock").generate_run(
            ctx.task_id, ctx.run_id, lease=lease
        )
        assert receipt.claim_ids == ()
        assert generator.calls == 0
        assert len(repo.saved) == 1

    asyncio.run(exercise())


def test_generation_without_current_lease_fails_closed():
    async def exercise():
        ctx, _ = fixture_context()
        with pytest.raises(ResearchClaimGenerationError, match="lease"):
            service = ResearchClaimGenerationService(RecordingRepository(ctx), FixedGenerator([]), model="mock")
            await service.generate_run(ctx.task_id, ctx.run_id, lease=None)

    asyncio.run(exercise())


def test_pipeline_verifying_generates_only_after_evidence_extraction():
    class EvidenceService:
        async def extract_run(self, task_id, run_id, *, lease=None):
            events.append(("evidence", task_id, run_id, lease))

    class ClaimService:
        async def generate_run(self, task_id, run_id, *, lease=None):
            events.append(("claims", task_id, run_id, lease))

    async def exercise():
        task_id, run_id = uuid4(), uuid4()
        lease = ResearchRunLease(run_id, uuid4(), 3, datetime.now(UTC) + timedelta(seconds=30))
        runner = ResearchPipelineStageRunner(
            object(),  # type: ignore[arg-type]
            evidence_extraction_service=EvidenceService(),  # type: ignore[arg-type]
            claim_generation_service=ClaimService(),  # type: ignore[arg-type]
        )
        await runner.run_stage(task_id, run_id, ResearchRunStatus.VERIFYING, lease=lease)
        assert events == [("evidence", task_id, run_id, lease), ("claims", task_id, run_id, lease)]

    events = []
    asyncio.run(exercise())
