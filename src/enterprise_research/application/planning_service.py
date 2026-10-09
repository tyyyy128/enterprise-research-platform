"""Application service for creating research plans."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.errors import (
    ResearchPlanningError,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.plan_repository import (
    ResearchPlanRepository,
)
from enterprise_research.application.planner import (
    ResearchPlanner,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.domain import (
    ResearchPlan,
    ResearchRunStatus,
)


class ResearchPlanningService:
    """Coordinate planning for one existing research run."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
        plan_repository: ResearchPlanRepository,
        planner: ResearchPlanner,
    ) -> None:
        self._research_repository = research_repository
        self._run_repository = run_repository
        self._plan_repository = plan_repository
        self._planner = planner

    async def create_plan(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        lease: ResearchRunLease | None = None,
    ) -> ResearchPlan:
        """Create a structured plan for a run in PLANNING state."""
        aggregate = await self._research_repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = await self._run_repository.get(run_id)

        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")

        if run.status != ResearchRunStatus.PLANNING:
            raise ResearchPlanningError("Research run must be in 'planning' state before creating a plan.")

        existing_plan = await self._plan_repository.get_for_run(run_id)

        if existing_plan is not None:
            if existing_plan.task_id != task_id:
                raise ResearchPlanningError("Persisted research plan belongs to a different task.")

            return existing_plan

        plan = await self._planner.plan(
            task=aggregate.task,
            run=run,
        )

        if plan.task_id != task_id or plan.run_id != run_id:
            raise ResearchPlanningError("Planner returned a plan for a different task or run.")

        if lease is None:
            await self._plan_repository.save(plan)
        else:
            await self._plan_repository.save_guarded(plan, lease, run_repository=self._run_repository)

        return plan
