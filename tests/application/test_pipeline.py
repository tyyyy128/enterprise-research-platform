from __future__ import annotations

import asyncio

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchPipelineStageRunner,
    ResearchPlanningService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
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


class DeterministicPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        self.calls += 1

        return ResearchPlan(
            task_id=task.task_id,
            run_id=run.run_id,
            objective=f"Research {task.query}",
            questions=[
                ResearchQuestion(
                    question=("What authoritative evidence addresses the research query?"),
                    rationale=("Establish the evidence base."),
                )
            ],
        )


class FailingPlanner:
    async def plan(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        raise RuntimeError("planner unavailable")


async def wait_until_idle(
    executor: ResearchRunExecutor,
) -> None:
    for _ in range(100):
        if executor.active_count == 0:
            return

        await asyncio.sleep(0)

    raise AssertionError("ResearchRunExecutor did not become idle.")


def test_executor_runs_planning_and_persists_plan() -> None:
    async def run() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        plan_repository = InMemoryResearchPlanRepository()

        planner = DeterministicPlanner()

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

        stage_runner = ResearchPipelineStageRunner(planning_service)

        executor = ResearchRunExecutor(
            run_application,
            stage_runner,
        )

        aggregate = await application.create_task("LangGraph durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await wait_until_idle(executor)

        persisted = await plan_repository.get_for_run(research_run.run_id)

        completed = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert persisted is not None
        assert persisted.task_id == aggregate.task.task_id
        assert persisted.run_id == research_run.run_id

        assert planner.calls == 1

        assert completed.status == ResearchRunStatus.COMPLETED

    asyncio.run(run())


def test_planning_failure_marks_run_failed() -> None:
    async def run() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        plan_repository = InMemoryResearchPlanRepository()

        application = ResearchApplicationService(research_repository)

        run_application = ResearchRunApplicationService(
            research_repository,
            run_repository,
        )

        planning_service = ResearchPlanningService(
            research_repository,
            run_repository,
            plan_repository,
            FailingPlanner(),
        )

        executor = ResearchRunExecutor(
            run_application,
            ResearchPipelineStageRunner(planning_service),
        )

        aggregate = await application.create_task("LangGraph durable execution")

        research_run = await run_application.create_run(aggregate.task.task_id)

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await wait_until_idle(executor)

        failed = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        persisted = await plan_repository.get_for_run(research_run.run_id)

        assert failed.status == ResearchRunStatus.FAILED
        assert failed.error == "planner unavailable"
        assert persisted is None

    asyncio.run(run())
