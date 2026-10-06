"""Research pipeline stage orchestration."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.planning_service import (
    ResearchPlanningService,
)
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
    ) -> None:
        self._planning_service = planning_service
        self._search_service = search_service

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        """Execute the work associated with one pipeline stage."""
        if stage == ResearchRunStatus.PLANNING:
            await self._planning_service.create_plan(
                task_id,
                run_id,
            )
            return

        if stage == ResearchRunStatus.SEARCHING and self._search_service is not None:
            await self._search_service.search_run(
                task_id,
                run_id,
            )
