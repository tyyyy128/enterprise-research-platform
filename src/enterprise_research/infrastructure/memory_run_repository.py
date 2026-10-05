"""In-memory repository for research execution runs."""

from __future__ import annotations

import asyncio
from uuid import UUID

from enterprise_research.domain import ResearchRun


class InMemoryResearchRunRepository:
    """Process-local research run repository used by tests and dev."""

    def __init__(self) -> None:
        self._items: dict[UUID, ResearchRun] = {}
        self._lock = asyncio.Lock()

    async def save(self, run: ResearchRun) -> None:
        """Persist an isolated copy of a research run."""
        async with self._lock:
            self._items[run.run_id] = run.model_copy(deep=True)

    async def get(
        self,
        run_id: UUID,
    ) -> ResearchRun | None:
        """Return an isolated copy of a research run."""
        async with self._lock:
            run = self._items.get(run_id)

            if run is None:
                return None

            return run.model_copy(deep=True)
