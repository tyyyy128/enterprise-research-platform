from __future__ import annotations

import asyncio
from uuid import uuid4

from enterprise_research.domain import ResearchAggregate
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
)


def test_repository_returns_none_for_unknown_task() -> None:
    async def run() -> None:
        repository = InMemoryResearchRepository()

        assert await repository.get(uuid4()) is None

    asyncio.run(run())


def test_repository_can_save_and_load_aggregate() -> None:
    async def run() -> None:
        repository = InMemoryResearchRepository()

        aggregate = ResearchAggregate.create("Research LangGraph durable execution")

        await repository.save(aggregate)

        loaded = await repository.get(aggregate.task.task_id)

        assert loaded is not None
        assert loaded.task.task_id == aggregate.task.task_id
        assert loaded.task.query == aggregate.task.query

    asyncio.run(run())


def test_repository_returns_deep_copy() -> None:
    async def run() -> None:
        repository = InMemoryResearchRepository()

        aggregate = ResearchAggregate.create("Original query")

        await repository.save(aggregate)

        loaded = await repository.get(aggregate.task.task_id)

        assert loaded is not None

        loaded.task.query = "Mutated query"

        loaded_again = await repository.get(aggregate.task.task_id)

        assert loaded_again is not None
        assert loaded_again.task.query == "Original query"

    asyncio.run(run())
