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
from enterprise_research.application.report_writing import (
    ReportClaimReference,
    ReportEvidenceReference,
    ResearchRunReportArtifact,
    render_verified_report,
    report_digest,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import evidence_fingerprint
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import ClaimStatus, ResearchRunStatus
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanRow,
    ResearchRunReportRow,
    ResearchSourceRow,
    ResearchStageAttemptRow,
)
from enterprise_research.infrastructure.postgres.stage_completion import validate_persisted_stage_artifacts


def _artifact(row: ResearchRunReportRow) -> ResearchRunReportArtifact:
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
    )


async def _make_run_draft(session: AsyncSession, *, task_id: UUID, run_id: UUID, fencing_token: int):
    """Rebuild the whole report from independently persisted, locked facts."""
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
        raise ResearchReportWritingError(
            "WRITING requires at least one supported Claim; automatic Claim verification is not implemented."
        )

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
    return render_verified_report(plan.objective, tuple(selected))


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
    draft = await _make_run_draft(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
    if (
        row.title != draft.title
        or row.body != draft.body
        or row.citations != list(draft.citations)
        or row.body_sha256 != draft.body_sha256
        or row.payload_sha256 != draft.payload_sha256
        or row.payload_sha256 != report_digest(row.title, row.body, tuple(row.citations))
    ):
        raise ResearchReportWritingError("WRITING report content/citations changed after the durable receipt.")


class PostgresResearchRunReportRepository:
    """Create once under live WRITING lease; never borrow task-level reports."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
            return _artifact(row)
