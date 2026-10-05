"""Contract for research planning adapters."""

from __future__ import annotations

from typing import Protocol

from enterprise_research.domain import (
    ResearchPlan,
    ResearchRun,
    ResearchTask,
)


class ResearchPlanner(Protocol):
    """Produce a structured plan for one research run."""

    async def plan(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        """Create a structured research plan."""
