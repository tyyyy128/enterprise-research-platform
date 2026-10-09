"""Repository contract for structured research plans."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import ResearchRunRepository
from enterprise_research.domain import ResearchPlan


class ResearchPlanRepository(Protocol):
    """Persistence contract for structured research plans."""

    async def save(
        self,
        plan: ResearchPlan,
    ) -> None:
        """Persist one structured research plan."""

    async def save_guarded(
        self,
        plan: ResearchPlan,
        lease: ResearchRunLease,
        *,
        run_repository: ResearchRunRepository,
    ) -> None:
        """Persist a plan only while holding the current PLANNING lease."""

    async def get_for_run(
        self,
        run_id: UUID,
    ) -> ResearchPlan | None:
        """Return the plan associated with one research run."""
