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

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """Return detached nonterminal snapshots ordered by update time/ID."""
        if limit < 1:
            raise ValueError("Recovery scan limit must be positive.")

        async with self._lock:
            unfinished = sorted(
                (
                    run
                    for run in self._items.values()
                    if not run.is_terminal and (task_id is None or run.task_id == task_id)
                ),
                key=lambda run: (run.updated_at, run.run_id),
            )
            return [run.model_copy(deep=True) for run in unfinished[:limit]]
