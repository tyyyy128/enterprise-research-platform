"""Repository contract for structured research plans."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.domain import ResearchPlan


class ResearchPlanRepository(Protocol):
    """Persistence contract for structured research plans."""

    async def save(
        self,
        plan: ResearchPlan,
    ) -> None:
        """Persist one structured research plan."""

    async def get_for_run(
        self,
        run_id: UUID,
    ) -> ResearchPlan | None:
        """Return the plan associated with one research run."""
