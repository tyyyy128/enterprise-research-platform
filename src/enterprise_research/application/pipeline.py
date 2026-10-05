"""Research pipeline stage orchestration."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.planning_service import (
    ResearchPlanningService,
)
from enterprise_research.domain import ResearchRunStatus


class ResearchPipelineStageRunner:
    """Dispatch concrete work for research execution stages."""

    def __init__(
        self,
        planning_service: ResearchPlanningService,
    ) -> None:
        self._planning_service = planning_service

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
