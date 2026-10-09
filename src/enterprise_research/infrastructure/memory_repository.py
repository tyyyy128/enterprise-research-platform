"""In-memory repository used during early development and tests."""

from __future__ import annotations

import asyncio
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import ResearchRunRepository
from enterprise_research.domain import ResearchAggregate, ResearchRunStatus


class InMemoryResearchRepository:
    """Process-local research aggregate repository."""

    def __init__(self) -> None:
        self._items: dict[UUID, ResearchAggregate] = {}
        self._lock = asyncio.Lock()

    async def save(self, aggregate: ResearchAggregate) -> None:
        async with self._lock:
            self._items[aggregate.task.task_id] = aggregate.model_copy(deep=True)

    async def save_guarded(
        self,
        aggregate: ResearchAggregate,
        lease: ResearchRunLease,
        stage: ResearchRunStatus,
        *,
        run_repository: ResearchRunRepository,
    ) -> None:
        async with run_repository.guarded_side_effect(lease, stage, task_id=aggregate.task.task_id):
            await self.save(aggregate)

    async def get(
        self,
        task_id: UUID,
    ) -> ResearchAggregate | None:
        async with self._lock:
            aggregate = self._items.get(task_id)

            if aggregate is None:
                return None

            return aggregate.model_copy(deep=True)
