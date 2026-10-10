from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from enterprise_research.application.errors import ResearchStageUnavailableError
from enterprise_research.application.pipeline import (
    ResearchPipelineStageRunner,
)
from enterprise_research.domain import ResearchRunStatus


class RecordingPlanningService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def create_plan(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        self.calls.append((task_id, run_id))


class RecordingSearchService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def search_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        self.calls.append((task_id, run_id))


class RecordingRetrievalService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def retrieve_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        self.calls.append((task_id, run_id))


class FailingRetrievalService:
    async def retrieve_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        raise RuntimeError("retrieval failed")


def test_pipeline_dispatches_extracting_to_retrieval() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()
        retrieval = RecordingRetrievalService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
            retrieval,  # type: ignore[arg-type]
        )

        task_id = uuid4()
        run_id = uuid4()

        await runner.run_stage(
            task_id,
            run_id,
            ResearchRunStatus.EXTRACTING,
        )

        assert retrieval.calls == [(task_id, run_id)]

        assert planning.calls == []
        assert search.calls == []

    asyncio.run(run())


def test_pipeline_rejects_extracting_when_retrieval_is_disabled() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
        )

        with pytest.raises(ResearchStageUnavailableError, match="EXTRACTING"):
            await runner.run_stage(uuid4(), uuid4(), ResearchRunStatus.EXTRACTING)

        assert planning.calls == []
        assert search.calls == []

    asyncio.run(run())


def test_pipeline_propagates_retrieval_failure() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
            FailingRetrievalService(),  # type: ignore[arg-type]
        )

        with pytest.raises(
            RuntimeError,
            match="retrieval failed",
        ):
            await runner.run_stage(
                uuid4(),
                uuid4(),
                ResearchRunStatus.EXTRACTING,
            )

    asyncio.run(run())
