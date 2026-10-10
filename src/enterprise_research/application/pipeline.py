"""Research pipeline stage orchestration."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.claim_generation import ResearchClaimGenerationService
from enterprise_research.application.claim_verification import ResearchClaimVerificationService
from enterprise_research.application.errors import ResearchStageUnavailableError
from enterprise_research.application.evidence_extraction_service import (
    ResearchEvidenceExtractionService,
)
from enterprise_research.application.evidence_progress import EvidenceProgressCallback
from enterprise_research.application.planning_service import (
    ResearchPlanningService,
)
from enterprise_research.application.report_writing import ResearchReportWritingService
from enterprise_research.application.retrieval_service import (
    ResearchRetrievalService,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.search_service import (
    ResearchSearchService,
)
from enterprise_research.domain import ResearchRunStatus


class ResearchPipelineStageRunner:
    """Dispatch concrete work for research execution stages."""

    def __init__(
        self,
        planning_service: ResearchPlanningService,
        search_service: ResearchSearchService | None = None,
        retrieval_service: ResearchRetrievalService | None = None,
        evidence_extraction_service: (ResearchEvidenceExtractionService | None) = None,
        evidence_progress_callback: EvidenceProgressCallback | None = None,
        report_writing_service: ResearchReportWritingService | None = None,
        claim_generation_service: ResearchClaimGenerationService | None = None,
        claim_verification_service: ResearchClaimVerificationService | None = None,
    ) -> None:
        self._planning_service = planning_service
        self._search_service = search_service
        self._retrieval_service = retrieval_service
        self._evidence_extraction_service = evidence_extraction_service
        self._evidence_progress_callback = evidence_progress_callback
        self._report_writing_service = report_writing_service
        self._claim_generation_service = claim_generation_service
        self._claim_verification_service = claim_verification_service

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
        *,
        lease: ResearchRunLease | None = None,
    ) -> None:
        """Execute the work associated with one pipeline stage."""
        if stage == ResearchRunStatus.PLANNING:
            if lease is None:
                await self._planning_service.create_plan(task_id, run_id)
            else:
                await self._planning_service.create_plan(task_id, run_id, lease=lease)
            return

        if stage == ResearchRunStatus.SEARCHING:
            if self._search_service is None:
                raise ResearchStageUnavailableError("SEARCHING requires a configured search service; no work was done.")
            if lease is None:
                await self._search_service.search_run(task_id, run_id)
            else:
                await self._search_service.search_run(task_id, run_id, lease=lease)
            return

        if stage == ResearchRunStatus.EXTRACTING:
            if self._retrieval_service is None:
                raise ResearchStageUnavailableError(
                    "EXTRACTING requires a configured retrieval service; no work was done."
                )
            if lease is None:
                await self._retrieval_service.retrieve_run(task_id, run_id)
            else:
                await self._retrieval_service.retrieve_run(task_id, run_id, lease=lease)
            return

        if stage == ResearchRunStatus.VERIFYING:
            if self._evidence_extraction_service is None:
                raise ResearchStageUnavailableError(
                    "VERIFYING requires a configured evidence service; no work was done."
                )
            if self._evidence_progress_callback is None:
                if lease is None:
                    await self._evidence_extraction_service.extract_run(task_id, run_id)
                else:
                    await self._evidence_extraction_service.extract_run(task_id, run_id, lease=lease)
            elif lease is None:
                await self._evidence_extraction_service.extract_run(
                    task_id,
                    run_id,
                    progress_callback=self._evidence_progress_callback,
                )
            else:
                await self._evidence_extraction_service.extract_run(
                    task_id,
                    run_id,
                    progress_callback=self._evidence_progress_callback,
                    lease=lease,
                )

            if self._claim_generation_service is not None:
                await self._claim_generation_service.generate_run(task_id, run_id, lease=lease)
            if self._claim_verification_service is not None:
                await self._claim_verification_service.verify_run(task_id, run_id, lease=lease)
            return

        if stage == ResearchRunStatus.WRITING:
            if self._report_writing_service is None:
                raise ResearchStageUnavailableError(
                    "WRITING has no report writer configured; a Run cannot complete without its report."
                )
            await self._report_writing_service.write_run(task_id, run_id, lease=lease)
            return

        raise ResearchStageUnavailableError(f"Unsupported research stage: {stage!s}.")
