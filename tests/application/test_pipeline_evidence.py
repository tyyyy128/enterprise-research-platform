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


class RecordingEvidenceExtractionService:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def extract_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        self.calls.append((task_id, run_id))


class FailingEvidenceExtractionService:
    async def extract_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        del task_id, run_id
        raise RuntimeError("evidence extraction failed")


def test_pipeline_dispatches_verifying_to_evidence_extraction() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()
        retrieval = RecordingRetrievalService()
        evidence = RecordingEvidenceExtractionService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
            retrieval,  # type: ignore[arg-type]
            evidence,  # type: ignore[arg-type]
        )

        task_id = uuid4()
        run_id = uuid4()

        await runner.run_stage(
            task_id,
            run_id,
            ResearchRunStatus.VERIFYING,
        )

        assert evidence.calls == [(task_id, run_id)]

        assert planning.calls == []
        assert search.calls == []
        assert retrieval.calls == []

    asyncio.run(run())


def test_pipeline_rejects_verifying_when_evidence_is_disabled() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()
        retrieval = RecordingRetrievalService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
            retrieval,  # type: ignore[arg-type]
        )

        with pytest.raises(ResearchStageUnavailableError, match="VERIFYING"):
            await runner.run_stage(uuid4(), uuid4(), ResearchRunStatus.VERIFYING)

        assert planning.calls == []
        assert search.calls == []
        assert retrieval.calls == []

    asyncio.run(run())


def test_pipeline_propagates_evidence_extraction_failure() -> None:
    async def run() -> None:
        planning = RecordingPlanningService()
        search = RecordingSearchService()
        retrieval = RecordingRetrievalService()
        evidence = FailingEvidenceExtractionService()

        runner = ResearchPipelineStageRunner(
            planning,  # type: ignore[arg-type]
            search,  # type: ignore[arg-type]
            retrieval,  # type: ignore[arg-type]
            evidence,  # type: ignore[arg-type]
        )

        with pytest.raises(
            RuntimeError,
            match="evidence extraction failed",
        ):
            await runner.run_stage(
                uuid4(),
                uuid4(),
                ResearchRunStatus.VERIFYING,
            )

    asyncio.run(run())


def test_pipeline_forwards_live_evidence_progress_callback() -> None:
    class CallbackEvidenceExtractionService:
        def __init__(self) -> None:
            self.received_callback = None

        async def extract_run(self, task_id, run_id, *, progress_callback=None):
            self.received_callback = progress_callback
            if progress_callback is not None:
                await progress_callback((task_id, run_id))

    async def run() -> None:
        evidence = CallbackEvidenceExtractionService()
        emitted = []

        async def on_progress(progress):
            emitted.append(progress)

        runner = ResearchPipelineStageRunner(
            RecordingPlanningService(),  # type: ignore[arg-type]
            evidence_extraction_service=evidence,  # type: ignore[arg-type]
            evidence_progress_callback=on_progress,  # type: ignore[arg-type]
        )
        task_id, run_id = uuid4(), uuid4()
        await runner.run_stage(task_id, run_id, ResearchRunStatus.VERIFYING)
        assert evidence.received_callback is on_progress
        assert emitted == [(task_id, run_id)]

    asyncio.run(run())
