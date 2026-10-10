"""7A: an absent handler is a persisted stage failure, never a completed research Run."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchPipelineStageRunner,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchStageOutcome,
    ResearchStageUnavailableError,
    SkeletonResearchStageRunner,
)
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure import InMemoryResearchRepository, InMemoryResearchRunRepository


class RecordingConfiguredServices:
    """Earlier stages are test doubles; only missing stage dispatch is under test."""

    async def create_plan(self, task_id: UUID, run_id: UUID, *, lease=None) -> None:
        pass

    async def search_run(self, task_id: UUID, run_id: UUID, *, lease=None) -> None:
        pass

    async def retrieve_run(self, task_id: UUID, run_id: UUID, *, lease=None) -> None:
        pass

    async def extract_run(self, task_id: UUID, run_id: UUID, *, progress_callback=None, lease=None) -> None:
        pass


async def _wait_idle(executor: ResearchRunExecutor) -> None:
    async def poll() -> None:
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


@pytest.mark.parametrize(
    ("unavailable", "expected_message"),
    [
        (ResearchRunStatus.SEARCHING, "SEARCHING requires"),
        (ResearchRunStatus.EXTRACTING, "EXTRACTING requires"),
        (ResearchRunStatus.VERIFYING, "VERIFYING requires"),
        (ResearchRunStatus.WRITING, "WRITING has no report writer"),
    ],
)
def test_disabled_stage_persists_failed_journal_instead_of_run_completed(unavailable, expected_message) -> None:
    async def exercise() -> None:
        research = InMemoryResearchRepository()
        runs = ResearchRunApplicationService(research, InMemoryResearchRunRepository())
        application = ResearchApplicationService(research)
        aggregate = await application.create_task(f"Missing handler: {unavailable.value}")
        run = await runs.create_run(aggregate.task.task_id)
        stub = RecordingConfiguredServices()
        stages = (
            ResearchRunStatus.PLANNING,
            ResearchRunStatus.SEARCHING,
            ResearchRunStatus.EXTRACTING,
            ResearchRunStatus.VERIFYING,
            ResearchRunStatus.WRITING,
        )
        selected = stages.index(unavailable)
        runner = ResearchPipelineStageRunner(
            stub,  # type: ignore[arg-type]
            stub if selected > 1 else None,  # type: ignore[arg-type]
            stub if selected > 2 else None,  # type: ignore[arg-type]
            evidence_extraction_service=stub if selected > 3 else None,  # type: ignore[arg-type]
        )
        executor = ResearchRunExecutor(runs, runner)
        executor.start(aggregate.task.task_id, run.run_id)
        await _wait_idle(executor)

        persisted = await runs.get_run(aggregate.task.task_id, run.run_id)
        assert persisted.status == ResearchRunStatus.FAILED
        assert expected_message in persisted.error
        attempts = await runs.inspect_stage_attempts(aggregate.task.task_id, run.run_id)
        assert [(entry.stage, entry.outcome) for entry in attempts] == [
            *((stage, ResearchStageOutcome.COMPLETED) for stage in stages[:selected]),
            (unavailable, ResearchStageOutcome.FAILED),
        ]
        assert expected_message in attempts[-1].error
        await executor.shutdown()

    asyncio.run(exercise())


def test_skeleton_stage_is_not_a_successful_noop() -> None:
    async def exercise() -> None:
        research = InMemoryResearchRepository()
        runs = ResearchRunApplicationService(research, InMemoryResearchRunRepository())
        aggregate = await ResearchApplicationService(research).create_task("Disabled planner")
        run = await runs.create_run(aggregate.task.task_id)
        executor = ResearchRunExecutor(runs, SkeletonResearchStageRunner())
        executor.start(aggregate.task.task_id, run.run_id)
        await _wait_idle(executor)
        persisted = await runs.get_run(aggregate.task.task_id, run.run_id)
        assert persisted.status == ResearchRunStatus.FAILED
        assert "skeleton runner" in persisted.error
        attempts = await runs.inspect_stage_attempts(aggregate.task.task_id, run.run_id)
        assert [(a.stage, a.outcome) for a in attempts] == [(ResearchRunStatus.PLANNING, ResearchStageOutcome.FAILED)]
        await executor.shutdown()

    asyncio.run(exercise())


def test_writing_and_terminal_states_cannot_silently_return() -> None:
    async def exercise() -> None:
        runner = ResearchPipelineStageRunner(RecordingConfiguredServices())  # type: ignore[arg-type]
        from uuid import uuid4

        for stage in (ResearchRunStatus.WRITING, ResearchRunStatus.COMPLETED, ResearchRunStatus.FAILED):
            with pytest.raises(ResearchStageUnavailableError):
                await runner.run_stage(uuid4(), uuid4(), stage)

    asyncio.run(exercise())
