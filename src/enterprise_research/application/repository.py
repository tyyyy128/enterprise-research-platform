"""Repository contracts for research aggregates."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import ResearchRunRepository
from enterprise_research.domain import ResearchAggregate, ResearchRunStatus


class ResearchRepository(Protocol):
    """Persistence contract for research aggregates."""

    async def save(self, aggregate: ResearchAggregate) -> None:
        """Persist a research aggregate."""

    async def save_guarded(
        self,
        aggregate: ResearchAggregate,
        lease: ResearchRunLease,
        stage: ResearchRunStatus,
        *,
        run_repository: ResearchRunRepository,
    ) -> None:
        """Save business data under a live stage lease at the commit boundary."""

    async def get(self, task_id: UUID) -> ResearchAggregate | None:
        """Return a research aggregate by task ID."""
