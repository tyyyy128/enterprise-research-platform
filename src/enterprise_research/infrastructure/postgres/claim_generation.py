"""3F: fenced, write-once candidate Claim persistence and input revalidation.

The expensive model call happens outside a database transaction. Before any
insert, this repository reacquires the Run lock and RECOMPUTES the exact input
snapshot from the durable 7B evidence manifest; all inserts share that lock.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application.claim_generation import (
    ClaimCandidate,
    ClaimGenerationContext,
    ClaimGenerationLimits,
    ClaimGenerationReceipt,
    ClaimInputEvidence,
    candidate_id,
    candidate_output_digest,
    sha256_json,
    validate_claim_candidates,
)
from enterprise_research.application.errors import ResearchClaimGenerationError
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import evidence_fingerprint
from enterprise_research.domain import ClaimStatus, ResearchRunStatus
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanQuestionRow,
    ResearchPlanRow,
    ResearchRunClaimGenerationRow,
    ResearchStageAttemptRow,
    ResearchTaskRow,
)
from enterprise_research.infrastructure.postgres.stage_completion import validate_persisted_stage_artifacts


async def _read_context(
    session: AsyncSession,
    task_id: UUID,
    run_id: UUID,
    lease: ResearchRunLease,
    limits: ClaimGenerationLimits,
) -> ClaimGenerationContext:
    await lock_fenced_stage(session, lease, task_id=task_id, stage=ResearchRunStatus.VERIFYING)
    journal = await session.get(
        ResearchStageAttemptRow, (run_id, ResearchRunStatus.VERIFYING.value, lease.fencing_token), with_for_update=True
    )
    if journal is None or journal.outcome != "started":
        raise ResearchClaimGenerationError("Claim generation requires a STARTED VERIFYING journal for this lease.")
    # 7B verifies the complete run-scoped document and evidence inventories.
    await validate_persisted_stage_artifacts(
        session,
        task_id=task_id,
        run_id=run_id,
        stage=ResearchRunStatus.VERIFYING,
        fencing_token=lease.fencing_token,
    )
    task = await session.get(ResearchTaskRow, task_id, with_for_update=True)
    plan = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id))
    manifest = await session.get(ResearchEvidenceManifestRow, run_id, with_for_update=True)
    if task is None or plan is None or manifest is None:
        raise ResearchClaimGenerationError("Run claim generation requires task, plan and evidence receipt.")
    questions = (
        await session.scalars(
            select(ResearchPlanQuestionRow)
            .where(ResearchPlanQuestionRow.plan_id == plan.id)
            .order_by(ResearchPlanQuestionRow.position)
            .with_for_update(read=True)
        )
    ).all()
    if not questions:
        raise ResearchClaimGenerationError("Claim generation requires nonempty persisted research questions.")
    evidence_ids = tuple(sorted((UUID(item) for item in manifest.evidence_ids), key=str))
    selected_ids = evidence_ids[: limits.max_evidence]
    selected = (
        (
            await session.scalars(
                select(ResearchEvidenceRow)
                .where(ResearchEvidenceRow.task_id == task_id, ResearchEvidenceRow.id.in_(selected_ids))
                .order_by(ResearchEvidenceRow.id)
                .with_for_update(read=True)
            )
        ).all()
        if selected_ids
        else []
    )
    if len(selected) != len(selected_ids):
        raise ResearchClaimGenerationError("Claim input evidence disappeared after verifying its receipt.")
    evidence = tuple(
        ClaimInputEvidence(
            evidence_id=row.id,
            source_id=row.source_id,
            excerpt=row.content[: limits.max_excerpt_chars],
            locator=row.locator,
            fingerprint=evidence_fingerprint(row.source_id, row.locator, row.content),
        )
        for row in selected
    )
    if {item.evidence_id for item in evidence} != set(selected_ids):
        raise ResearchClaimGenerationError("Claim input evidence selection is inconsistent.")
    digest = sha256_json(
        {
            "task_id": str(task_id),
            "run_id": str(run_id),
            "generation": lease.fencing_token,
            "task_query": task.query,
            "objective": plan.objective,
            "questions": [(str(q.id), q.question) for q in questions],
            "document_hashes": manifest.document_hashes,
            "evidence_hashes": manifest.evidence_hashes,
            "selected": [
                [str(item.evidence_id), str(item.source_id), item.locator, item.excerpt, item.fingerprint]
                for item in evidence
            ],
            "available_evidence_count": len(evidence_ids),
            "limits": [limits.max_evidence, limits.max_excerpt_chars],
        }
    )
    return ClaimGenerationContext(
        task_id=task_id,
        run_id=run_id,
        fencing_token=lease.fencing_token,
        task_query=task.query,
        objective=plan.objective,
        questions=tuple(q.question for q in questions),
        evidence=evidence,
        available_evidence_count=len(evidence_ids),
        input_sha256=digest,
    )


class PostgresResearchClaimGenerationRepository:
    """Persist UNSUPPORTED candidates and a run-specific receipt in one tx."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease, limits: ClaimGenerationLimits
    ) -> ClaimGenerationContext:
        async with self._session_factory() as session:
            async with session.begin():
                return await _read_context(session, task_id, run_id, lease, limits)

    async def save_guarded(
        self,
        context: ClaimGenerationContext,
        lease: ResearchRunLease,
        limits: ClaimGenerationLimits,
        candidates: tuple[ClaimCandidate, ...],
        *,
        model: str,
    ) -> ClaimGenerationReceipt:
        if not model.strip() or len(model) > 128:
            raise ResearchClaimGenerationError("Claim generator model identifier is invalid.")
        async with self._session_factory() as session:
            async with session.begin():
                current = await _read_context(session, context.task_id, context.run_id, lease, limits)
                if current != context:
                    raise ResearchClaimGenerationError("Claim generation input changed during the external model call.")
                candidates = validate_claim_candidates(current, candidates, limits)
                previous = await session.get(ResearchRunClaimGenerationRow, current.run_id, with_for_update=True)
                if previous is not None:
                    raise ResearchClaimGenerationError("Claim generation receipt is write-once for each Run.")
                claim_ids = tuple(candidate_id(current.run_id, candidate) for candidate in candidates)
                for candidate, claim_id in zip(candidates, claim_ids, strict=True):
                    # A proposed claim is NEVER trusted as supported, even
                    # when the model reports high confidence or a citation.
                    session.add(
                        ResearchClaimRow(
                            id=claim_id,
                            task_id=current.task_id,
                            text=candidate.text,
                            status=ClaimStatus.UNSUPPORTED.value,
                            confidence=0.0,
                        )
                    )
                await session.flush()
                for candidate, claim_id in zip(candidates, claim_ids, strict=True):
                    for position, evidence_id in enumerate(candidate.evidence_ids):
                        session.add(ClaimEvidenceRow(claim_id=claim_id, evidence_id=evidence_id, position=position))
                receipt = ClaimGenerationReceipt(
                    task_id=current.task_id,
                    run_id=current.run_id,
                    fencing_token=current.fencing_token,
                    input_sha256=current.input_sha256,
                    output_sha256=candidate_output_digest(current.run_id, candidates),
                    available_evidence_count=current.available_evidence_count,
                    selected_evidence_ids=tuple(item.evidence_id for item in current.evidence),
                    claim_ids=claim_ids,
                    generator_model=model,
                )
                session.add(
                    ResearchRunClaimGenerationRow(
                        run_id=current.run_id,
                        task_id=current.task_id,
                        fencing_token=current.fencing_token,
                        input_sha256=current.input_sha256,
                        output_sha256=receipt.output_sha256,
                        available_evidence_count=current.available_evidence_count,
                        selected_evidence_ids=[str(item) for item in receipt.selected_evidence_ids],
                        claim_ids=[str(item) for item in claim_ids],
                        candidate_count=len(claim_ids),
                        generator_model=model,
                        created_at=func.clock_timestamp(),
                    )
                )
                await session.flush()
                return receipt
