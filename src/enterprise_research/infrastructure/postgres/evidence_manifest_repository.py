"""Run-scoped evidence completion receipt persistence under a fenced lease."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application.evidence_manifest_repository import ResearchEvidenceManifestRepository
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import EvidenceCompletionManifest
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import ResearchEvidenceManifestRow


class PostgresResearchEvidenceManifestRepository(ResearchEvidenceManifestRepository):
    """Write-once per Run; a stale generation cannot alter a prior receipt."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_guarded(self, manifest: EvidenceCompletionManifest, lease: ResearchRunLease) -> None:
        if manifest.run_id != lease.run_id or manifest.fencing_token != lease.fencing_token:
            raise ValueError("Evidence completion receipt identity does not match the lease.")
        async with self._session_factory() as session:
            async with session.begin():
                await lock_fenced_stage(session, lease, task_id=manifest.task_id, stage=ResearchRunStatus.VERIFYING)
                if await session.get(ResearchEvidenceManifestRow, manifest.run_id) is not None:
                    raise ValueError("Evidence completion receipt already exists for this research run.")
                session.add(
                    ResearchEvidenceManifestRow(
                        run_id=manifest.run_id,
                        task_id=manifest.task_id,
                        fencing_token=manifest.fencing_token,
                        eligible_document_count=manifest.eligible_document_count,
                        scheduled_chunk_count=manifest.scheduled_chunk_count,
                        completed_chunk_count=manifest.completed_chunk_count,
                        verified_count=manifest.verified_count,
                        created_count=manifest.created_count,
                        reused_count=manifest.reused_count,
                        document_hashes={str(i): h for i, h in manifest.document_hashes},
                        evidence_ids=[str(i) for i in manifest.evidence_ids],
                        evidence_hashes={str(i): h for i, h in manifest.evidence_hashes},
                        completed_at=func.clock_timestamp(),
                    )
                )
