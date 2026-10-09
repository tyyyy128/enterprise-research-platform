"""In-memory repository for structured research plans."""

from __future__ import annotations

import asyncio
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import ResearchRunRepository
from enterprise_research.domain import ResearchPlan, ResearchRunStatus


class InMemoryResearchPlanRepository:
    """Process-local research plan repository."""

    def __init__(self) -> None:
        self._items: dict[UUID, ResearchPlan] = {}
        self._lock = asyncio.Lock()

    async def save(
        self,
        plan: ResearchPlan,
    ) -> None:
        """Persist an isolated copy of one research plan."""
        async with self._lock:
            existing = self._items.get(plan.run_id)

            if existing is not None and existing.plan_id != plan.plan_id:
                raise ValueError("Research run already has a different plan.")

            self._items[plan.run_id] = plan.model_copy(deep=True)

    async def save_guarded(
        self,
        plan: ResearchPlan,
        lease: ResearchRunLease,
        *,
        run_repository: ResearchRunRepository,
    ) -> None:
        async with run_repository.guarded_side_effect(lease, ResearchRunStatus.PLANNING, task_id=plan.task_id):
            await self.save(plan)

    async def get_for_run(
        self,
        run_id: UUID,
    ) -> ResearchPlan | None:
        """Return an isolated copy of one run's plan."""
        async with self._lock:
            plan = self._items.get(run_id)

            if plan is None:
                return None

            return plan.model_copy(deep=True)
