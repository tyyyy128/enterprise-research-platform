from __future__ import annotations

import asyncio
from uuid import UUID

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
)
from enterprise_research.domain import (
    ResearchRunStatus,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)


class RecordingStageRunner:
    def __init__(self) -> None:
        self.stages: list[ResearchRunStatus] = []

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        self.stages.append(stage)
        await asyncio.sleep(0)


class FailingStageRunner:
    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        if stage == ResearchRunStatus.SEARCHING:
            raise RuntimeError("search provider unavailable")

        await asyncio.sleep(0)


class EmptyErrorStageRunner:
    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        raise RuntimeError()


class BlockingStageRunner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        self.calls += 1
        self.entered.set()

        await self.release.wait()


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


async def wait_until_idle(
    executor: ResearchRunExecutor,
) -> None:
    for _ in range(100):
        if executor.active_count == 0:
            return

        await asyncio.sleep(0)

    raise AssertionError("ResearchRunExecutor did not become idle.")


def test_executor_completes_all_stages() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")
        research_run = await run_application.create_run(aggregate.task.task_id)

        stage_runner = RecordingStageRunner()
        executor = ResearchRunExecutor(
            run_application,
            stage_runner,
        )

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await wait_until_idle(executor)

        loaded = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert loaded.status == ResearchRunStatus.COMPLETED
        assert loaded.error is None
        assert stage_runner.stages == [
            ResearchRunStatus.PLANNING,
            ResearchRunStatus.SEARCHING,
            ResearchRunStatus.EXTRACTING,
            ResearchRunStatus.VERIFYING,
            ResearchRunStatus.WRITING,
        ]

    asyncio.run(run())


def test_executor_persists_stage_failure() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")
        research_run = await run_application.create_run(aggregate.task.task_id)

        executor = ResearchRunExecutor(
            run_application,
            FailingStageRunner(),
        )

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await wait_until_idle(executor)

        loaded = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert loaded.status == ResearchRunStatus.FAILED
        assert loaded.error == "search provider unavailable"
        assert loaded.completed_at is not None

    asyncio.run(run())


def test_empty_exception_uses_exception_type() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")
        research_run = await run_application.create_run(aggregate.task.task_id)

        executor = ResearchRunExecutor(
            run_application,
            EmptyErrorStageRunner(),
        )

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await wait_until_idle(executor)

        loaded = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert loaded.status == ResearchRunStatus.FAILED
        assert loaded.error == "RuntimeError"

    asyncio.run(run())


def test_duplicate_start_does_not_duplicate_active_run() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")
        research_run = await run_application.create_run(aggregate.task.task_id)

        stage_runner = BlockingStageRunner()
        executor = ResearchRunExecutor(
            run_application,
            stage_runner,
        )

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await stage_runner.entered.wait()

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert executor.active_count == 1
        assert stage_runner.calls == 1

        stage_runner.release.set()

        await wait_until_idle(executor)

    asyncio.run(run())


def test_shutdown_cancels_without_marking_failure() -> None:
    async def run() -> None:
        application, run_application = create_services()

        aggregate = await application.create_task("Research durable execution")
        research_run = await run_application.create_run(aggregate.task.task_id)

        stage_runner = BlockingStageRunner()
        executor = ResearchRunExecutor(
            run_application,
            stage_runner,
        )

        executor.start(
            aggregate.task.task_id,
            research_run.run_id,
        )

        await stage_runner.entered.wait()
        await executor.shutdown()

        loaded = await run_application.get_run(
            aggregate.task.task_id,
            research_run.run_id,
        )

        assert loaded.status == ResearchRunStatus.PLANNING
        assert loaded.error is None
        assert executor.active_count == 0

    asyncio.run(run())
