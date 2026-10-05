from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)


def create_services() -> tuple[
    ResearchApplicationService,
    ResearchRunApplicationService,
]:
    research_repository = InMemoryResearchRepository()

    return (
        ResearchApplicationService(research_repository),
        ResearchRunApplicationService(
            research_repository,
            InMemoryResearchRunRepository(),
        ),
    )


def test_create_and_get_research_run() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")

        created = await run_application.create_run(aggregate.task.task_id)

        loaded = await run_application.get_run(
            aggregate.task.task_id,
            created.run_id,
        )

        assert loaded == created
        assert loaded.status == ResearchRunStatus.PENDING

    asyncio.run(run())


def test_create_run_requires_existing_task() -> None:
    async def run() -> None:
        _, run_application = create_services()

        with pytest.raises(ResearchTaskNotFoundError):
            await run_application.create_run(uuid4())

    asyncio.run(run())


def test_get_unknown_run_raises() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")

        with pytest.raises(ResearchRunNotFoundError):
            await run_application.get_run(
                aggregate.task.task_id,
                uuid4(),
            )

    asyncio.run(run())


def test_run_is_scoped_to_owning_task() -> None:
    async def run() -> None:
        application, run_application = create_services()

        first = await application.create_task("First task")
        second = await application.create_task("Second task")

        research_run = await run_application.create_run(first.task.task_id)

        with pytest.raises(ResearchRunNotFoundError):
            await run_application.get_run(
                second.task.task_id,
                research_run.run_id,
            )

    asyncio.run(run())
