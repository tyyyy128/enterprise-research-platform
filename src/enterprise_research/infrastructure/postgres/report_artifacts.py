"""7C: fenced run-level report writer and terminal completion proof.

The report is a write-once *snapshot* of supported, run-verified claims, not
an alias of the task-level report. All reads used in deciding completion and
the final status update execute with the same leased Run row lock/transaction.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application.errors import ResearchReportWritingError
from enterprise_research.application.report_synthesis import (
    ReportSynthesisCaveat,
    ReportSynthesisContext,
    ReportSynthesisPlan,
    ReportSynthesisQuote,
    plan_data,
    plan_from_data,
    synthesis_input_sha256,
    synthesis_receipt_sha256,
    validate_synthesis_plan,
)
from enterprise_research.application.report_writing import (
    ReportClaimReference,
    ReportEvidenceReference,
    ResearchRunReportArtifact,
    render_synthesized_report,
    render_verified_report,
    report_digest,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import evidence_fingerprint
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import ClaimStatus, ResearchRunStatus
from enterprise_research.infrastructure.postgres.claim_verification import validate_run_claim_verification_receipt
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanRow,
    ResearchRunClaimGenerationRow,
    ResearchRunClaimVerificationRow,
    ResearchRunReportRow,
    ResearchRunReportSynthesisRow,
    ResearchSourceRow,
    ResearchStageAttemptRow,
)
from enterprise_research.infrastructure.postgres.stage_completion import validate_persisted_stage_artifacts


def _artifact(
    row: ResearchRunReportRow, synthesis: ResearchRunReportSynthesisRow | None = None
) -> ResearchRunReportArtifact:
    return ResearchRunReportArtifact(
        task_id=row.task_id,
        run_id=row.run_id,
        fencing_token=row.fencing_token,
        title=row.title,
        body=row.body,
        citations=tuple(row.citations),
        body_sha256=row.body_sha256,
        payload_sha256=row.payload_sha256,
        created_at=row.created_at,
        synthesis_model=synthesis.model if synthesis is not None else None,
        synthesis_sha256=synthesis.receipt_sha256 if synthesis is not None else None,
    )


async def _make_run_material(session: AsyncSession, *, task_id: UUID, run_id: UUID, fencing_token: int):
    """Rebuild the whole material from independently persisted, locked facts."""
    verified = await session.get(
        ResearchStageAttemptRow, (run_id, ResearchRunStatus.VERIFYING.value, fencing_token), with_for_update=True
    )
    if verified is None or verified.outcome != ResearchStageOutcome.COMPLETED.value:
        raise ResearchReportWritingError("WRITING requires a completed VERIFYING journal for this generation.")
    await validate_persisted_stage_artifacts(
        session, task_id=task_id, run_id=run_id, stage=ResearchRunStatus.VERIFYING, fencing_token=fencing_token
    )
    manifest = await session.get(ResearchEvidenceManifestRow, run_id, with_for_update=True)
    assert manifest is not None  # Established by VERIFYING proof in this same transaction.
    ids = set(manifest.evidence_ids)
    if not ids:
        raise ResearchReportWritingError("WRITING has no verified evidence; do not invent a supported report.")

    plan = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id))
    if plan is None or not plan.objective.strip():
        raise ResearchReportWritingError("WRITING has no persisted run-specific plan objective.")
    claims = (
        await session.scalars(
            select(ResearchClaimRow)
            .where(ResearchClaimRow.task_id == task_id, ResearchClaimRow.status == ClaimStatus.SUPPORTED.value)
            .order_by(ResearchClaimRow.id)
            .with_for_update(read=True)
        )
    ).all()
    if not claims:
        raise ResearchReportWritingError("WRITING requires at least one supported, evidence-verified Claim.")

    # If this Run has 3F candidate provenance, only its independently 3G-
    # verified SUPPORTED candidates may enter the final report. Task-level
    # manual/older SUPPORTED Claims cannot impersonate this Run's decisions.
    generation = await session.get(ResearchRunClaimGenerationRow, run_id, with_for_update=True)
    caveats: list[ReportSynthesisCaveat] = []
    adverse: list[dict] = []
    if generation is not None:
        await validate_run_claim_verification_receipt(
            session, task_id=task_id, run_id=run_id, fencing_token=fencing_token, required=True
        )
        verification = await session.get(ResearchRunClaimVerificationRow, run_id, with_for_update=True)
        assert verification is not None
        verified_ids = {
            item["claim_id"] for item in verification.verdicts if item["status"] == ClaimStatus.SUPPORTED.value
        }
        claims = [claim for claim in claims if str(claim.id) in verified_ids]
        if not claims:
            raise ResearchReportWritingError("WRITING found no SUPPORTED candidates verified for this Run.")
        adverse = [item for item in verification.verdicts if item["status"] != ClaimStatus.SUPPORTED.value]

    links = (
        await session.scalars(
            select(ClaimEvidenceRow)
            .where(ClaimEvidenceRow.claim_id.in_([claim.id for claim in claims]))
            .order_by(ClaimEvidenceRow.claim_id, ClaimEvidenceRow.position)
            .with_for_update(read=True)
        )
    ).all()
    by_claim: dict[UUID, list[UUID]] = defaultdict(list)
    for link in links:
        by_claim[link.claim_id].append(link.evidence_id)

    evidence_rows = (
        await session.scalars(
            select(ResearchEvidenceRow)
            .where(ResearchEvidenceRow.task_id == task_id, ResearchEvidenceRow.id.in_([UUID(i) for i in ids]))
            .with_for_update(read=True)
        )
    ).all()
    evidence_by_id = {row.id: row for row in evidence_rows}
    source_ids = {row.source_id for row in evidence_rows}
    sources = (
        await session.scalars(
            select(ResearchSourceRow)
            .where(ResearchSourceRow.task_id == task_id, ResearchSourceRow.id.in_(source_ids))
            .with_for_update(read=True)
        )
    ).all()
    source_by_id = {row.id: row for row in sources}

    if adverse:
        raw_claims = (
            await session.scalars(
                select(ResearchClaimRow)
                .where(
                    ResearchClaimRow.task_id == task_id,
                    ResearchClaimRow.id.in_([UUID(item["claim_id"]) for item in adverse]),
                )
                .with_for_update(read=True)
            )
        ).all()
        texts = {str(item.id): item.text for item in raw_claims}
        if len(texts) != len(adverse):
            raise ResearchReportWritingError("WRITING cannot locate all adverse Claim decisions.")

        def quote_refs(quotes: list) -> tuple[ReportSynthesisQuote, ...]:
            result = []
            for evidence_id, quoted_text in quotes:
                evidence = evidence_by_id.get(UUID(evidence_id))
                if evidence is None or quoted_text not in evidence.content:
                    raise ResearchReportWritingError("WRITING adverse verification quote changed or disappeared.")
                source = source_by_id.get(evidence.source_id)
                if source is None or not source.url:
                    raise ResearchReportWritingError("WRITING adverse quote has no source.")
                result.append(
                    ReportSynthesisQuote(
                        evidence.id,
                        quoted_text,
                        source.url,
                        evidence_fingerprint(evidence.source_id, evidence.locator, evidence.content),
                    )
                )
            return tuple(result)

        caveats = [
            ReportSynthesisCaveat(
                UUID(item["claim_id"]),
                texts[item["claim_id"]],
                item["status"],
                item["rationale"],
                quote_refs(item["supporting"]),
                quote_refs(item["contradicting"]),
            )
            for item in adverse
        ]

    selected: list[ReportClaimReference] = []
    for claim in claims:
        claim_evidence = by_claim.get(claim.id, [])
        overlap = any(str(evidence_id) in ids for evidence_id in claim_evidence)
        if not overlap:
            continue  # A task-level claim from another Run is not this Run's report.
        if not claim_evidence or any(str(evidence_id) not in ids for evidence_id in claim_evidence):
            raise ResearchReportWritingError("WRITING rejects supported claims mixing this Run with other evidence.")
        refs = []
        for evidence_id in claim_evidence:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise ResearchReportWritingError("WRITING claim references missing or foreign Evidence.")
            source = source_by_id.get(evidence.source_id)
            if source is None or not source.url:
                raise ResearchReportWritingError("WRITING requires linked, task-owned source URLs.")
            fingerprint = evidence_fingerprint(evidence.source_id, evidence.locator, evidence.content)
            if manifest.evidence_hashes.get(str(evidence_id)) != fingerprint:
                raise ResearchReportWritingError("WRITING evidence differs from the verified Run receipt.")
            refs.append(
                ReportEvidenceReference(
                    evidence_id=evidence.id,
                    source_id=evidence.source_id,
                    source_url=source.url,
                    locator=evidence.locator,
                    excerpt=evidence.content,
                    fingerprint=fingerprint,
                )
            )
        selected.append(ReportClaimReference(claim_id=claim.id, text=claim.text, evidence=tuple(refs)))
    if not selected:
        raise ResearchReportWritingError("WRITING found no supported Claim grounded exclusively in this Run.")
    return plan.objective, tuple(selected), tuple(caveats)


async def _make_run_draft(session: AsyncSession, *, task_id: UUID, run_id: UUID, fencing_token: int):
    """Preserve 7C deterministic report semantics for legacy configured writers."""
    title, claims, _ = await _make_run_material(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
    return render_verified_report(title, claims)


def _context(task_id: UUID, run_id: UUID, fencing_token: int, material: tuple) -> ReportSynthesisContext:
    title, claims, caveats = material
    return ReportSynthesisContext(
        task_id,
        run_id,
        fencing_token,
        title,
        claims,
        caveats,
        synthesis_input_sha256(title, claims, caveats),
    )


async def validate_run_terminal_report(
    session: AsyncSession,
    *,
    task_id: UUID,
    run_id: UUID,
    fencing_token: int,
    require_writing_journal: bool,
) -> None:
    """A write-once report + verified citations must match live business facts.

    Caller already locked Run and uses this same transaction to finish the
    WRITING journal or to update Run WRITING -> COMPLETED.
    """
    if require_writing_journal:
        journal = await session.get(
            ResearchStageAttemptRow,
            (run_id, ResearchRunStatus.WRITING.value, fencing_token),
            with_for_update=True,
        )
        if journal is None or journal.outcome != ResearchStageOutcome.COMPLETED.value:
            raise ResearchReportWritingError("Terminal completion requires a completed WRITING journal for this token.")
    row = await session.get(ResearchRunReportRow, run_id, with_for_update=True)
    if row is None or row.task_id != task_id or row.fencing_token != fencing_token:
        raise ResearchReportWritingError("WRITING proof missing: run/generation-scoped report artifact.")
    synthesis = await session.get(ResearchRunReportSynthesisRow, run_id, with_for_update=True)
    if synthesis is None:
        draft = await _make_run_draft(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
    else:
        await _require_3g_synthesis_receipts(session, run_id)
        if synthesis.task_id != task_id or synthesis.fencing_token != fencing_token:
            raise ResearchReportWritingError("WRITING synthesis receipt has foreign Run/Fencing ownership.")
        context = _context(
            task_id,
            run_id,
            fencing_token,
            await _make_run_material(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token),
        )
        if context.input_sha256 != synthesis.input_sha256:
            raise ResearchReportWritingError("WRITING synthesis input changed after the model snapshot.")
        plan = validate_synthesis_plan(context, plan_from_data(synthesis.plan))
        if synthesis_receipt_sha256(synthesis.model, context.input_sha256, plan) != synthesis.receipt_sha256:
            raise ResearchReportWritingError("WRITING synthesis receipt checksum changed.")
        draft = render_synthesized_report(context, plan)
    if (
        row.title != draft.title
        or row.body != draft.body
        or row.citations != list(draft.citations)
        or row.body_sha256 != draft.body_sha256
        or row.payload_sha256 != draft.payload_sha256
        or row.payload_sha256 != report_digest(row.title, row.body, tuple(row.citations))
    ):
        raise ResearchReportWritingError("WRITING report content/citations changed after the durable receipt.")


async def _require_3g_synthesis_receipts(session: AsyncSession, run_id: UUID) -> None:
    """3H has a stricter admission gate than the legacy manual 7C report mode."""
    generation = await session.get(ResearchRunClaimGenerationRow, run_id, with_for_update=True)
    verification = await session.get(ResearchRunClaimVerificationRow, run_id, with_for_update=True)
    if generation is None or verification is None:
        raise ResearchReportWritingError("WRITING synthesis requires 3F/3G Run-level verification receipts.")


class PostgresResearchRunReportRepository:
    """Create once under live WRITING lease; never borrow task-level reports."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_synthesis_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease
    ) -> ReportSynthesisContext:
        if lease.run_id != run_id:
            raise ResearchReportWritingError("WRITING synthesis lease does not match this Run.")
        async with self._session_factory() as session:
            async with session.begin():
                await lock_fenced_stage(session, lease, task_id=task_id, stage=ResearchRunStatus.WRITING)
                journal = await session.get(
                    ResearchStageAttemptRow,
                    (run_id, ResearchRunStatus.WRITING.value, lease.fencing_token),
                    with_for_update=True,
                )
                if journal is None or journal.outcome != ResearchStageOutcome.STARTED.value:
                    raise ResearchReportWritingError("WRITING synthesis requires a started Stage Journal.")
                if await session.get(ResearchRunReportRow, run_id) is not None:
                    raise ResearchReportWritingError("WRITING report is already committed for this Run.")
                await _require_3g_synthesis_receipts(session, run_id)
                material = await _make_run_material(
                    session, task_id=task_id, run_id=run_id, fencing_token=lease.fencing_token
                )
                # Fail before network I/O if the verified input exceeds the old
                # evidence/citation budgets; never feed a truncated set to the model.
                render_verified_report(material[0], material[1])
                return _context(task_id, run_id, lease.fencing_token, material)

    async def write_synthesized_guarded(
        self,
        context: ReportSynthesisContext,
        plan: ReportSynthesisPlan,
        lease: ResearchRunLease,
        *,
        model: str,
    ) -> None:
        """No model I/O here: revalidate snapshot under lock and atomically write two receipts."""
        if lease.run_id != context.run_id or lease.fencing_token != context.fencing_token:
            raise ResearchReportWritingError("WRITING synthesis lease changed during model execution.")
        validate_synthesis_plan(context, plan)
        async with self._session_factory() as session:
            async with session.begin():
                await lock_fenced_stage(session, lease, task_id=context.task_id, stage=ResearchRunStatus.WRITING)
                journal = await session.get(
                    ResearchStageAttemptRow,
                    (context.run_id, ResearchRunStatus.WRITING.value, lease.fencing_token),
                    with_for_update=True,
                )
                if journal is None or journal.outcome != ResearchStageOutcome.STARTED.value:
                    raise ResearchReportWritingError("WRITING synthesis Stage Journal is not started.")
                if await session.get(ResearchRunReportRow, context.run_id) is not None:
                    raise ResearchReportWritingError("WRITING report is immutable and already exists for this Run.")
                await _require_3g_synthesis_receipts(session, context.run_id)
                current = _context(
                    context.task_id,
                    context.run_id,
                    lease.fencing_token,
                    await _make_run_material(
                        session, task_id=context.task_id, run_id=context.run_id, fencing_token=lease.fencing_token
                    ),
                )
                if current.input_sha256 != context.input_sha256 or current != context:
                    raise ResearchReportWritingError("WRITING synthesis inputs changed during model execution.")
                draft = render_synthesized_report(current, plan)
                session.add(
                    ResearchRunReportRow(
                        run_id=context.run_id,
                        task_id=context.task_id,
                        fencing_token=lease.fencing_token,
                        title=draft.title,
                        body=draft.body,
                        citations=list(draft.citations),
                        body_sha256=draft.body_sha256,
                        payload_sha256=draft.payload_sha256,
                        created_at=func.clock_timestamp(),
                    )
                )
                session.add(
                    ResearchRunReportSynthesisRow(
                        run_id=context.run_id,
                        task_id=context.task_id,
                        fencing_token=lease.fencing_token,
                        model=model,
                        input_sha256=current.input_sha256,
                        plan=plan_data(plan),
                        receipt_sha256=synthesis_receipt_sha256(model, current.input_sha256, plan),
                        created_at=func.clock_timestamp(),
                    )
                )
                await session.flush()

    async def write_guarded(self, task_id: UUID, run_id: UUID, lease: ResearchRunLease) -> ResearchRunReportArtifact:
        if lease.run_id != run_id:
            raise ResearchReportWritingError("WRITING lease does not match target Run.")
        async with self._session_factory() as session:
            async with session.begin():
                await lock_fenced_stage(session, lease, task_id=task_id, stage=ResearchRunStatus.WRITING)
                journal = await session.get(
                    ResearchStageAttemptRow,
                    (run_id, ResearchRunStatus.WRITING.value, lease.fencing_token),
                    with_for_update=True,
                )
                if journal is None or journal.outcome != ResearchStageOutcome.STARTED.value:
                    raise ResearchReportWritingError("WRITING requires a started journal attempt.")
                if await session.get(ResearchRunReportRow, run_id) is not None:
                    raise ResearchReportWritingError("WRITING report is immutable and already exists for this Run.")
                draft = await _make_run_draft(
                    session, task_id=task_id, run_id=run_id, fencing_token=lease.fencing_token
                )
                row = ResearchRunReportRow(
                    run_id=run_id,
                    task_id=task_id,
                    fencing_token=lease.fencing_token,
                    title=draft.title,
                    body=draft.body,
                    citations=list(draft.citations),
                    body_sha256=draft.body_sha256,
                    payload_sha256=draft.payload_sha256,
                    created_at=func.clock_timestamp(),
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return _artifact(row)

    async def get(self, task_id: UUID, run_id: UUID) -> ResearchRunReportArtifact | None:
        async with self._session_factory() as session:
            row = await session.get(ResearchRunReportRow, run_id)
            if row is None or row.task_id != task_id:
                return None
            if (
                report_digest(row.title, row.body, tuple(row.citations)) != row.payload_sha256
                or hashlib.sha256(row.body.encode("utf-8")).hexdigest() != row.body_sha256
            ):
                raise ResearchReportWritingError("Stored report artifact checksum does not match content.")
            synthesis = await session.get(ResearchRunReportSynthesisRow, run_id)
            if synthesis is not None and (
                synthesis.task_id != task_id
                or synthesis.fencing_token != row.fencing_token
                or synthesis_receipt_sha256(synthesis.model, synthesis.input_sha256, plan_from_data(synthesis.plan))
                != synthesis.receipt_sha256
            ):
                raise ResearchReportWritingError("Stored report synthesis receipt is inconsistent.")
            return _artifact(row, synthesis)
