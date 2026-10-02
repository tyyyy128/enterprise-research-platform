"""Repository contracts for research aggregates."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.domain import ResearchAggregate


class ResearchRepository(Protocol):
    """Persistence contract for research aggregates."""

    async def save(self, aggregate: ResearchAggregate) -> None:
        """Persist a research aggregate."""

    async def get(self, task_id: UUID) -> ResearchAggregate | None:
        """Return a research aggregate by task ID."""
