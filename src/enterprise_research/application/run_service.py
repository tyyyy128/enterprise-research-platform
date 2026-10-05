"""Application service for research execution runs."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.errors import (
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.domain import ResearchRun


class ResearchRunApplicationService:
    """Coordinates research run lifecycle use cases."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
    ) -> None:
        self._research_repository = research_repository
        self._run_repository = run_repository

    async def create_run(
        self,
        task_id: UUID,
    ) -> ResearchRun:
        """Create a pending run for an existing research task."""
        aggregate = await self._research_repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = ResearchRun.create(task_id)

        await self._run_repository.save(run)

        return run

    async def get_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> ResearchRun:
        """Return one run scoped to its owning research task."""
        run = await self._run_repository.get(run_id)

        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")

        return run
