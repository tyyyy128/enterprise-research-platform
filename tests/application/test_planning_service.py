from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchPlanningError,
    ResearchPlanningService,
    ResearchRunApplicationService,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
    ResearchTask,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)


class RecordingPlanner:
    """Deterministic planner used by application tests."""

    def __init__(
        self,
        *,
        wrong_task: bool = False,
        wrong_run: bool = False,
    ) -> None:
        self.calls: list[tuple[UUID, UUID, str]] = []

        self._wrong_task = wrong_task
        self._wrong_run = wrong_run

    async def plan(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        self.calls.append(
            (
                task.task_id,
                run.run_id,
                task.query,
            )
        )

        return ResearchPlan(
            task_id=(uuid4() if self._wrong_task else task.task_id),
            run_id=(uuid4() if self._wrong_run else run.run_id),
            objective=f"Research {task.query}",
            questions=[
                ResearchQuestion(
                    question=(f"What primary evidence addresses {task.query}?"),
                    rationale=("Establish an evidence-backed answer."),
                )
            ],
        )


def create_services(
    planner: RecordingPlanner | None = None,
) -> tuple[
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchPlanningService,
    RecordingPlanner,
]:
    research_repository = InMemoryResearchRepository()
    run_repository = InMemoryResearchRunRepository()
    plan_repository = InMemoryResearchPlanRepository()

    planner = planner or RecordingPlanner()

    return (
        ResearchApplicationService(research_repository),
        ResearchRunApplicationService(
            research_repository,
            run_repository,
        ),
        ResearchPlanningService(
            research_repository,
            run_repository,
            plan_repository,
            planner,
        ),
        planner,
    )


def test_create_plan_calls_planner_for_planning_run() -> None:
    async def run() -> None:
        (
            application,
            run_application,
            planning_service,
            planner,
        ) = create_services()

        aggregate = await application.create_task("LangGraph durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        await run_application.advance_run(
            aggregate.task.task_id,
            research_run.run_id,
            ResearchRunStatus.PLANNING,
        )

        plan = await planning_service.create_plan(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert plan.task_id == aggregate.task.task_id
        assert plan.run_id == research_run.run_id

        assert planner.calls == [
            (
                aggregate.task.task_id,
                research_run.run_id,
                aggregate.task.query,
            )
        ]

    asyncio.run(run())


def test_create_plan_requires_existing_task() -> None:
    async def run() -> None:
        (
            _,
            _,
            planning_service,
            _,
        ) = create_services()

        with pytest.raises(
            ResearchTaskNotFoundError,
        ):
            await planning_service.create_plan(
                uuid4(),
                uuid4(),
            )

    asyncio.run(run())


def test_create_plan_requires_existing_run() -> None:
    async def run() -> None:
        (
            application,
            _,
            planning_service,
            _,
        ) = create_services()

        aggregate = await application.create_task("Research durable execution")

        with pytest.raises(
            ResearchRunNotFoundError,
        ):
            await planning_service.create_plan(
                aggregate.task.task_id,
                uuid4(),
            )

    asyncio.run(run())


def test_create_plan_rejects_run_from_another_task() -> None:
    async def run() -> None:
        (
            application,
            run_application,
            planning_service,
            _,
        ) = create_services()

        first = await application.create_task("First research task")
        second = await application.create_task("Second research task")

        research_run = await run_application.create_run(first.task.task_id)

        await run_application.advance_run(
            first.task.task_id,
            research_run.run_id,
            ResearchRunStatus.PLANNING,
        )

        with pytest.raises(
            ResearchRunNotFoundError,
        ):
            await planning_service.create_plan(
                second.task.task_id,
                research_run.run_id,
            )

    asyncio.run(run())


def test_create_plan_requires_planning_state() -> None:
    async def run() -> None:
        (
            application,
            run_application,
            planning_service,
            _,
        ) = create_services()

        aggregate = await application.create_task("Research durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        assert research_run.status == ResearchRunStatus.PENDING

        with pytest.raises(
            ResearchPlanningError,
            match="planning",
        ):
            await planning_service.create_plan(
                aggregate.task.task_id,
                research_run.run_id,
            )

    asyncio.run(run())


def test_create_plan_rejects_mismatched_planner_output() -> None:
    async def run() -> None:
        planner = RecordingPlanner(wrong_run=True)

        (
            application,
            run_application,
            planning_service,
            _,
        ) = create_services(planner)

        aggregate = await application.create_task("Research durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        await run_application.advance_run(
            aggregate.task.task_id,
            research_run.run_id,
            ResearchRunStatus.PLANNING,
        )

        with pytest.raises(
            ResearchPlanningError,
            match="different task or run",
        ):
            await planning_service.create_plan(
                aggregate.task.task_id,
                research_run.run_id,
            )

    asyncio.run(run())


def test_create_plan_persists_validated_plan() -> None:
    async def run() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        plan_repository = InMemoryResearchPlanRepository()
        planner = RecordingPlanner()

        application = ResearchApplicationService(research_repository)
        run_application = ResearchRunApplicationService(
            research_repository,
            run_repository,
        )
        planning_service = ResearchPlanningService(
            research_repository,
            run_repository,
            plan_repository,
            planner,
        )

        aggregate = await application.create_task("Research durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        await run_application.advance_run(
            aggregate.task.task_id,
            research_run.run_id,
            ResearchRunStatus.PLANNING,
        )

        created = await planning_service.create_plan(
            aggregate.task.task_id,
            research_run.run_id,
        )

        persisted = await plan_repository.get_for_run(research_run.run_id)

        assert persisted == created

    asyncio.run(run())


def test_existing_plan_is_reused_without_calling_planner() -> None:
    async def run() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        plan_repository = InMemoryResearchPlanRepository()

        class UnexpectedPlanner:
            async def plan(
                self,
                *,
                task: ResearchTask,
                run: ResearchRun,
            ) -> ResearchPlan:
                raise AssertionError("Planner must not be called when a persisted plan exists.")

        application = ResearchApplicationService(research_repository)

        run_application = ResearchRunApplicationService(
            research_repository,
            run_repository,
        )

        planning_service = ResearchPlanningService(
            research_repository,
            run_repository,
            plan_repository,
            UnexpectedPlanner(),
        )

        aggregate = await application.create_task("Research durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        await run_application.advance_run(
            aggregate.task.task_id,
            research_run.run_id,
            ResearchRunStatus.PLANNING,
        )

        existing = ResearchPlan(
            task_id=aggregate.task.task_id,
            run_id=research_run.run_id,
            objective="Existing durable plan.",
            questions=[ResearchQuestion(question="How does recovery work?")],
        )

        await plan_repository.save(existing)

        returned = await planning_service.create_plan(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert returned == existing

    asyncio.run(run())
