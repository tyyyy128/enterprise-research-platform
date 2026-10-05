from __future__ import annotations

import asyncio
from uuid import uuid4

from enterprise_research.domain import (
    ResearchRun,
    ResearchRunStatus,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRunRepository,
)


def test_run_repository_returns_none_for_unknown_run() -> None:
    async def run() -> None:
        repository = InMemoryResearchRunRepository()

        assert await repository.get(uuid4()) is None

    asyncio.run(run())


def test_run_repository_can_save_and_load_run() -> None:
    async def run() -> None:
        repository = InMemoryResearchRunRepository()
        research_run = ResearchRun.create(uuid4())

        await repository.save(research_run)

        loaded = await repository.get(research_run.run_id)

        assert loaded is not None
        assert loaded == research_run

    asyncio.run(run())


def test_run_repository_returns_deep_copy() -> None:
    async def run() -> None:
        repository = InMemoryResearchRunRepository()
        research_run = ResearchRun.create(uuid4())

        await repository.save(research_run)

        loaded = await repository.get(research_run.run_id)

        assert loaded is not None

        loaded.status = ResearchRunStatus.FAILED
        loaded.error = "mutated outside repository"

        loaded_again = await repository.get(research_run.run_id)

        assert loaded_again is not None
        assert loaded_again.status == ResearchRunStatus.PENDING
        assert loaded_again.error is None

    asyncio.run(run())
