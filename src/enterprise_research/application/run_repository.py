"""Repository contract for research execution runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.domain import ResearchRun


class ResearchRunRepository(Protocol):
    """Persistence contract for research execution lifecycle records."""

    async def save(self, run: ResearchRun) -> None:
        """Persist a research run."""

    async def get(self, run_id: UUID) -> ResearchRun | None:
        """Return a research run by ID."""

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """Oldest nonterminal runs first; read-only and bounded."""
