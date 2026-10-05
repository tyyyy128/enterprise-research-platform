from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)


def create_plan(
    run_id=None,
) -> ResearchPlan:
    return ResearchPlan(
        task_id=uuid4(),
        run_id=run_id or uuid4(),
        objective="Assess durable execution.",
        questions=[
            ResearchQuestion(question="How does recovery work?"),
            ResearchQuestion(question="What are the trade-offs?"),
        ],
    )


def test_unknown_run_has_no_plan() -> None:
    async def run() -> None:
        repository = InMemoryResearchPlanRepository()

        assert await repository.get_for_run(uuid4()) is None

    asyncio.run(run())


def test_save_and_load_preserves_order_and_isolation() -> None:
    async def run() -> None:
        repository = InMemoryResearchPlanRepository()

        plan = create_plan()

        await repository.save(plan)

        loaded = await repository.get_for_run(plan.run_id)

        assert loaded == plan

        assert [item.question for item in loaded.questions] == [
            "How does recovery work?",
            "What are the trade-offs?",
        ]

        loaded.objective = "Mutated outside repository"

        loaded_again = await repository.get_for_run(plan.run_id)

        assert loaded_again is not None
        assert loaded_again.objective == ("Assess durable execution.")

    asyncio.run(run())


def test_run_cannot_receive_a_different_plan() -> None:
    async def run() -> None:
        repository = InMemoryResearchPlanRepository()

        run_id = uuid4()

        first = create_plan(run_id)
        second = create_plan(run_id)

        await repository.save(first)

        with pytest.raises(
            ValueError,
            match="different plan",
        ):
            await repository.save(second)

    asyncio.run(run())
