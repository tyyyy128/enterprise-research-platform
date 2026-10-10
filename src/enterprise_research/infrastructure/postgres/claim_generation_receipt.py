"""Verify optional 3F claim receipts during the 7B VERIFYING commit.

A receipt is optional while the claim backend is disabled. Once one exists,
it must remain internally consistent with durable task Claims and run Evidence.
Changes to Claim status by a later verification stage are intentionally allowed.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_research.application.claim_generation import ClaimCandidate, candidate_id, candidate_output_digest
from enterprise_research.application.errors import ResearchClaimGenerationError
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceManifestRow,
    ResearchRunClaimGenerationRow,
)


async def validate_run_claim_receipt(session: AsyncSession, *, task_id: UUID, run_id: UUID, fencing_token: int) -> None:
    receipt = await session.get(ResearchRunClaimGenerationRow, run_id, with_for_update=True)
    if receipt is None:
        return
    manifest = await session.get(ResearchEvidenceManifestRow, run_id, with_for_update=True)
    if manifest is None or receipt.task_id != task_id or receipt.fencing_token != fencing_token:
        raise ResearchClaimGenerationError("VERIFYING claim receipt task/generation does not match the Run.")
    ids = receipt.claim_ids
    selected = receipt.selected_evidence_ids
    if (
        not isinstance(ids, list)
        or not isinstance(selected, list)
        or len(ids) != receipt.candidate_count
        or len(set(ids)) != len(ids)
        or len(set(selected)) != len(selected)
        or not set(selected).issubset(set(manifest.evidence_ids))
        or receipt.available_evidence_count != len(manifest.evidence_ids)
    ):
        raise ResearchClaimGenerationError("VERIFYING claim receipt inventory is inconsistent.")
    if not ids:
        if receipt.output_sha256 != candidate_output_digest(run_id, ()):
            raise ResearchClaimGenerationError("VERIFYING empty claim receipt has an invalid digest.")
        return
    try:
        claim_ids = [UUID(item) for item in ids]
    except ValueError as exc:
        raise ResearchClaimGenerationError("VERIFYING claim receipt contains invalid IDs.") from exc
    claims = (
        await session.scalars(
            select(ResearchClaimRow)
            .where(ResearchClaimRow.task_id == task_id, ResearchClaimRow.id.in_(claim_ids))
            .with_for_update(read=True)
        )
    ).all()
    if len(claims) != len(claim_ids):
        raise ResearchClaimGenerationError("VERIFYING receipt references deleted or foreign Claims.")
    by_id = {row.id: row for row in claims}
    links = (
        await session.scalars(
            select(ClaimEvidenceRow)
            .where(ClaimEvidenceRow.claim_id.in_(claim_ids))
            .order_by(ClaimEvidenceRow.claim_id, ClaimEvidenceRow.position)
            .with_for_update(read=True)
        )
    ).all()
    refs: dict[UUID, list[UUID]] = {claim_id: [] for claim_id in claim_ids}
    for link in links:
        refs[link.claim_id].append(link.evidence_id)
        if str(link.evidence_id) not in selected:
            raise ResearchClaimGenerationError("VERIFYING receipt Claim references out-of-scope Evidence.")
    candidates: list[ClaimCandidate] = []
    for claim_id in claim_ids:
        reference_ids = tuple(refs[claim_id])
        row = by_id[claim_id]
        if not reference_ids or len(reference_ids) != len(set(reference_ids)):
            raise ResearchClaimGenerationError("VERIFYING receipt Claim has invalid Evidence references.")
        candidate = ClaimCandidate(text=row.text, evidence_ids=reference_ids)
        if candidate_id(run_id, candidate) != claim_id:
            raise ResearchClaimGenerationError("VERIFYING receipt Claim text or references changed.")
        candidates.append(candidate)
    if candidate_output_digest(run_id, tuple(candidates)) != receipt.output_sha256:
        raise ResearchClaimGenerationError("VERIFYING receipt Claim digest no longer matches persistent data.")
