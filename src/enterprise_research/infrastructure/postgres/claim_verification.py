"""3G: fenced semantic verification and immutable Run verdict receipts.

The remote model is called OUTSIDE the transaction. The submission transaction
reacquires the Run row lock, rechecks every input and records Claim mutations
and the verifier receipt atomically. Stage/terminal proof later recomputes both
hashes and checks live Claim status against that receipt.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application.claim_generation import ClaimInputEvidence, sha256_json
from enterprise_research.application.claim_verification import (
    ClaimVerificationLimits,
    ClaimVerificationReceipt,
    ClaimVerificationVerdict,
    EvidenceQuote,
    VerificationClaim,
    VerificationContext,
    serialize_verdict,
    validate_verdicts,
    verification_receipt_digest,
)
from enterprise_research.application.errors import ResearchClaimVerificationError
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import evidence_fingerprint
from enterprise_research.domain import ClaimStatus, ResearchRunStatus
from enterprise_research.infrastructure.postgres.claim_generation_receipt import validate_run_claim_receipt
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanQuestionRow,
    ResearchPlanRow,
    ResearchRunClaimGenerationRow,
    ResearchRunClaimVerificationRow,
    ResearchStageAttemptRow,
    ResearchTaskRow,
)


async def read_verification_context(
    session: AsyncSession,
    *,
    task_id: UUID,
    run_id: UUID,
    fencing_token: int,
    limits: ClaimVerificationLimits,
) -> VerificationContext:
    """Caller holds Run lock; this helper never checks active stage itself."""
    await validate_run_claim_receipt(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
    generated = await session.get(ResearchRunClaimGenerationRow, run_id, with_for_update=True)
    manifest = await session.get(ResearchEvidenceManifestRow, run_id, with_for_update=True)
    task = await session.get(ResearchTaskRow, task_id, with_for_update=True)
    plan = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id))
    if generated is None or manifest is None or task is None or plan is None:
        raise ResearchClaimVerificationError("Claim verification requires a completed 3F generation receipt and plan.")
    if generated.task_id != task_id or generated.fencing_token != fencing_token:
        raise ResearchClaimVerificationError("Claim verification receipt does not match this Run/generation.")
    raw_claims = generated.claim_ids
    raw_evidence = generated.selected_evidence_ids
    if len(raw_claims) > limits.max_claims or len(raw_evidence) > limits.max_evidence:
        raise ResearchClaimVerificationError("Claim verification input exceeds the configured batch budget.")
    try:
        claim_ids = [UUID(value) for value in raw_claims]
        evidence_ids = [UUID(value) for value in raw_evidence]
    except (TypeError, ValueError) as exc:
        raise ResearchClaimVerificationError("Claim verification input has invalid ID values.") from exc
    claim_rows = (
        (
            await session.scalars(
                select(ResearchClaimRow)
                .where(ResearchClaimRow.task_id == task_id, ResearchClaimRow.id.in_(claim_ids))
                .with_for_update(read=True)
            )
        ).all()
        if claim_ids
        else []
    )
    if len(claim_rows) != len(claim_ids):
        raise ResearchClaimVerificationError("One or more candidate Claims are no longer persisted.")
    claim_by_id = {item.id: item for item in claim_rows}
    link_rows = (
        (
            await session.scalars(
                select(ClaimEvidenceRow)
                .where(ClaimEvidenceRow.claim_id.in_(claim_ids))
                .order_by(ClaimEvidenceRow.claim_id, ClaimEvidenceRow.position)
                .with_for_update(read=True)
            )
        ).all()
        if claim_ids
        else []
    )
    linked: dict[UUID, list[UUID]] = {item: [] for item in claim_ids}
    for item in link_rows:
        linked[item.claim_id].append(item.evidence_id)
    claims = tuple(VerificationClaim(item, claim_by_id[item].text, tuple(linked[item])) for item in claim_ids)
    evidence_rows = (
        (
            await session.scalars(
                select(ResearchEvidenceRow)
                .where(ResearchEvidenceRow.task_id == task_id, ResearchEvidenceRow.id.in_(evidence_ids))
                .with_for_update(read=True)
            )
        ).all()
        if evidence_ids
        else []
    )
    if len(evidence_rows) != len(evidence_ids):
        raise ResearchClaimVerificationError("Verification evidence inventory is incomplete.")
    evidence_by_id = {item.id: item for item in evidence_rows}
    evidence: list[ClaimInputEvidence] = []
    for item_id in evidence_ids:
        item = evidence_by_id[item_id]
        fingerprint = evidence_fingerprint(item.source_id, item.locator, item.content)
        if manifest.evidence_hashes.get(str(item_id)) != fingerprint:
            raise ResearchClaimVerificationError("Verification input Evidence fingerprint has changed.")
        evidence.append(
            ClaimInputEvidence(
                item.id, item.source_id, item.content[: limits.max_excerpt_chars], item.locator, fingerprint
            )
        )
    questions = (
        await session.scalars(
            select(ResearchPlanQuestionRow)
            .where(ResearchPlanQuestionRow.plan_id == plan.id)
            .order_by(ResearchPlanQuestionRow.position)
            .with_for_update(read=True)
        )
    ).all()
    digest = sha256_json(
        {
            "task_id": str(task_id),
            "run_id": str(run_id),
            "generation": fencing_token,
            "generation_input_sha256": generated.input_sha256,
            "generation_output_sha256": generated.output_sha256,
            "task_query": task.query,
            "objective": plan.objective,
            "questions": [[str(q.id), q.question] for q in questions],
            "manifest_documents": manifest.document_hashes,
            "manifest_evidence": manifest.evidence_hashes,
            "claims": [[str(c.claim_id), c.text, [str(i) for i in c.evidence_ids]] for c in claims],
            "evidence": [[str(e.evidence_id), str(e.source_id), e.locator, e.excerpt, e.fingerprint] for e in evidence],
            "max_excerpt_chars": limits.max_excerpt_chars,
        }
    )
    return VerificationContext(
        task_id,
        run_id,
        fencing_token,
        generated.input_sha256,
        generated.output_sha256,
        tuple(evidence),
        claims,
        digest,
    )


def _limits_payload(limits: ClaimVerificationLimits) -> dict[str, object]:
    return {
        "max_claims": limits.max_claims,
        "max_evidence": limits.max_evidence,
        "max_excerpt_chars": limits.max_excerpt_chars,
        "max_quote_chars": limits.max_quote_chars,
        "max_rationale_chars": limits.max_rationale_chars,
        "min_supported_confidence": limits.min_supported_confidence,
    }


def _decode_verdicts(payload: object) -> tuple[ClaimVerificationVerdict, ...]:
    if not isinstance(payload, list):
        raise ResearchClaimVerificationError("Claim verification receipt decisions must be an array.")
    try:
        result = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {
                "claim_id",
                "status",
                "confidence",
                "rationale",
                "supporting",
                "contradicting",
            }:
                raise ValueError("Invalid verifier decision object.")

            def decode_quotes(raw: object) -> tuple[EvidenceQuote, ...]:
                if not isinstance(raw, list):
                    raise ValueError("Invalid quote list.")
                if any(not isinstance(q, list) or len(q) != 2 or not isinstance(q[1], str) for q in raw):
                    raise ValueError("Invalid quote pair.")
                return tuple(EvidenceQuote(UUID(q[0]), q[1]) for q in raw)

            result.append(
                ClaimVerificationVerdict(
                    UUID(item["claim_id"]),
                    ClaimStatus(item["status"]),
                    item["confidence"],
                    item["rationale"],
                    decode_quotes(item["supporting"]),
                    decode_quotes(item["contradicting"]),
                )
            )
        return tuple(result)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ResearchClaimVerificationError("Claim verification receipt has corrupted structured verdicts.") from exc


async def validate_run_claim_verification_receipt(
    session: AsyncSession,
    *,
    task_id: UUID,
    run_id: UUID,
    fencing_token: int,
    required: bool = False,
) -> None:
    """Validate current business rows against immutable, attributable verdicts.

    Called inside stage/terminal completion while the same Run row is locked.
    The receipt remains optional when the 3G backend is disabled.
    """
    row = await session.get(ResearchRunClaimVerificationRow, run_id, with_for_update=True)
    if row is None:
        if required:
            raise ResearchClaimVerificationError("Claim verification receipt missing for this Run.")
        return
    if row.task_id != task_id or row.fencing_token != fencing_token:
        raise ResearchClaimVerificationError("Claim verification receipt Run/Fencing mismatch.")
    try:
        limits = ClaimVerificationLimits(**row.limits)
    except (ValueError, TypeError, KeyError) as exc:
        raise ResearchClaimVerificationError("Claim verification receipt contains invalid limits.") from exc
    context = await read_verification_context(
        session, task_id=task_id, run_id=run_id, fencing_token=fencing_token, limits=limits
    )
    if (
        row.input_sha256 != context.input_sha256
        or row.generation_output_sha256 != context.generation_output_sha256
        or row.generation_input_sha256 != context.generation_input_sha256
        or row.claim_ids != [str(item.claim_id) for item in context.claims]
    ):
        raise ResearchClaimVerificationError("Claim verification receipt input differs from current Run artifacts.")
    raw_verdicts = _decode_verdicts(row.verdicts)
    verdicts = validate_verdicts(context, raw_verdicts, limits)
    if [serialize_verdict(item) for item in verdicts] != row.verdicts or verification_receipt_digest(
        verdicts, model=row.verifier_model, limits=limits
    ) != row.verdicts_sha256:
        raise ResearchClaimVerificationError("Claim verification receipt verdict digest is inconsistent.")
    if len(context.claims) != row.candidate_count:
        raise ResearchClaimVerificationError("Claim verification receipt decision coverage has changed.")
    current_rows = (
        (
            await session.scalars(
                select(ResearchClaimRow)
                .where(
                    ResearchClaimRow.task_id == task_id, ResearchClaimRow.id.in_([item.claim_id for item in verdicts])
                )
                .with_for_update(read=True)
            )
        ).all()
        if verdicts
        else []
    )
    current = {item.id: item for item in current_rows}
    for item in verdicts:
        claim = current.get(item.claim_id)
        expected_confidence = item.confidence if item.status == ClaimStatus.SUPPORTED else 0.0
        if claim is None or claim.status != item.status.value or abs(claim.confidence - expected_confidence) > 1e-8:
            raise ResearchClaimVerificationError("Claim status/confidence no longer matches its verification receipt.")


class PostgresResearchClaimVerificationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def load_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease, limits: ClaimVerificationLimits
    ) -> VerificationContext:
        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_started(session, task_id, lease)
                from enterprise_research.infrastructure.postgres.stage_completion import (
                    validate_persisted_stage_artifacts,
                )

                await validate_persisted_stage_artifacts(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    stage=ResearchRunStatus.VERIFYING,
                    fencing_token=lease.fencing_token,
                )
                if await session.get(ResearchRunClaimVerificationRow, run_id) is not None:
                    raise ResearchClaimVerificationError("Claim verification is write-once per Run.")
                context = await read_verification_context(
                    session, task_id=task_id, run_id=run_id, fencing_token=lease.fencing_token, limits=limits
                )
                await self._require_pending(session, context)
                return context

    async def save_guarded(
        self,
        context: VerificationContext,
        lease: ResearchRunLease,
        limits: ClaimVerificationLimits,
        verdicts: tuple[ClaimVerificationVerdict, ...],
        *,
        model: str,
    ) -> ClaimVerificationReceipt:
        if not model.strip() or len(model) > 128:
            raise ResearchClaimVerificationError("Invalid claim verifier model identifier.")
        if lease.run_id != context.run_id or lease.fencing_token != context.fencing_token:
            raise ResearchClaimVerificationError("Claim verification context does not match the fenced lease.")
        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_started(session, context.task_id, lease)
                from enterprise_research.infrastructure.postgres.stage_completion import (
                    validate_persisted_stage_artifacts,
                )

                await validate_persisted_stage_artifacts(
                    session,
                    task_id=context.task_id,
                    run_id=context.run_id,
                    stage=ResearchRunStatus.VERIFYING,
                    fencing_token=lease.fencing_token,
                )
                if await session.get(ResearchRunClaimVerificationRow, context.run_id) is not None:
                    raise ResearchClaimVerificationError("Claim verification is write-once per Run.")
                current = await read_verification_context(
                    session,
                    task_id=context.task_id,
                    run_id=context.run_id,
                    fencing_token=lease.fencing_token,
                    limits=limits,
                )
                if current != context:
                    raise ResearchClaimVerificationError(
                        "Claim verification input changed during the external model call."
                    )
                await self._require_pending(session, context)
                verified = validate_verdicts(current, verdicts, limits)
                for item in verified:
                    claim = await session.get(ResearchClaimRow, item.claim_id, with_for_update=True)
                    assert claim is not None
                    claim.status = item.status.value
                    claim.confidence = item.confidence if item.status == ClaimStatus.SUPPORTED else 0.0
                row = ResearchRunClaimVerificationRow(
                    task_id=current.task_id,
                    run_id=current.run_id,
                    fencing_token=current.fencing_token,
                    generation_input_sha256=current.generation_input_sha256,
                    generation_output_sha256=current.generation_output_sha256,
                    input_sha256=current.input_sha256,
                    verdicts_sha256=verification_receipt_digest(verified, model=model, limits=limits),
                    candidate_count=len(verified),
                    claim_ids=[str(item.claim_id) for item in verified],
                    verdicts=[serialize_verdict(item) for item in verified],
                    verifier_model=model,
                    limits=_limits_payload(limits),
                    created_at=func.clock_timestamp(),
                )
                session.add(row)
                await session.flush()
                return ClaimVerificationReceipt(
                    current.task_id,
                    current.run_id,
                    current.fencing_token,
                    current.input_sha256,
                    row.verdicts_sha256,
                    tuple(item.claim_id for item in verified),
                    model,
                )

    @staticmethod
    async def _lock_started(session: AsyncSession, task_id: UUID, lease: ResearchRunLease) -> None:
        await lock_fenced_stage(session, lease, task_id=task_id, stage=ResearchRunStatus.VERIFYING)
        journal = await session.get(
            ResearchStageAttemptRow,
            (lease.run_id, ResearchRunStatus.VERIFYING.value, lease.fencing_token),
            with_for_update=True,
        )
        if journal is None or journal.outcome != "started":
            raise ResearchClaimVerificationError("Claim verification requires the current STARTED VERIFYING journal.")

    @staticmethod
    async def _require_pending(session: AsyncSession, context: VerificationContext) -> None:
        ids = [claim.claim_id for claim in context.claims]
        if not ids:
            return
        rows = (
            await session.scalars(
                select(ResearchClaimRow).where(ResearchClaimRow.id.in_(ids)).with_for_update(read=True)
            )
        ).all()
        if len(rows) != len(ids) or any(
            row.status != ClaimStatus.UNSUPPORTED.value or row.confidence != 0.0 for row in rows
        ):
            raise ResearchClaimVerificationError("Claim verification requires untouched, unverified 3F candidates.")
