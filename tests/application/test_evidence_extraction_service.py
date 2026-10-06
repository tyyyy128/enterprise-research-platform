from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from enterprise_research.application.document_chunking import (
    DocumentChunkingSettings,
)
from enterprise_research.application.errors import (
    ResearchEvidenceExtractionError,
)
from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
    EvidenceCandidate,
)
from enterprise_research.application.evidence_extraction_service import (
    EvidenceExtractionSummary,
    ResearchEvidenceExtractionService,
)
from enterprise_research.application.evidence_workload import (
    EvidenceWorkloadSettings,
)
from enterprise_research.application.run_service import (
    ResearchRunApplicationService,
)
from enterprise_research.application.service import (
    ResearchApplicationService,
)
from enterprise_research.domain import (
    Evidence,
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
    ResearchTask,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)


class StaticEvidenceExtractor:
    """Return deterministic candidates from each chunk."""

    def __init__(
        self,
        factory: Callable[
            [DocumentChunk],
            list[EvidenceCandidate],
        ],
    ) -> None:
        self._factory = factory
        self.calls: list[DocumentChunk] = []

    async def extract(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
        plan: ResearchPlan,
        source: Source,
        chunk: DocumentChunk,
    ) -> list[EvidenceCandidate]:
        del (
            task,
            run,
            plan,
            source,
        )

        self.calls.append(chunk)

        return self._factory(chunk)


@dataclass
class Setup:
    research_repository: InMemoryResearchRepository
    run_repository: InMemoryResearchRunRepository
    plan_repository: InMemoryResearchPlanRepository
    document_repository: InMemoryRetrievedDocumentRepository
    application: ResearchApplicationService
    aggregate: ResearchAggregate
    run: ResearchRun
    source: Source


async def setup_run(
    *,
    content: str,
    target_status: ResearchRunStatus = (ResearchRunStatus.VERIFYING),
) -> Setup:
    research_repository = InMemoryResearchRepository()

    run_repository = InMemoryResearchRunRepository()

    plan_repository = InMemoryResearchPlanRepository()

    document_repository = InMemoryRetrievedDocumentRepository()

    application = ResearchApplicationService(research_repository)

    run_application = ResearchRunApplicationService(
        research_repository,
        run_repository,
    )

    aggregate = await application.create_task("Research durable execution")

    source = Source(
        source_type=SourceType.WEB,
        title="Example source",
        url="https://example.com/article",
    )

    await application.add_source(
        aggregate.task.task_id,
        source,
    )

    aggregate = await application.get_task(aggregate.task.task_id)

    run = await run_application.create_run(aggregate.task.task_id)

    for stage in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
    ):
        run = await run_application.advance_run(
            aggregate.task.task_id,
            run.run_id,
            stage,
        )

        if stage == target_status:
            break

    plan = ResearchPlan(
        task_id=aggregate.task.task_id,
        run_id=run.run_id,
        objective=("Assess durable execution and recovery."),
        questions=[ResearchQuestion(question=("How does recovery work?"))],
    )

    await plan_repository.save(plan)

    await document_repository.save(
        RetrievedDocument.succeeded(
            source_id=source.source_id,
            requested_url=str(source.url),
            final_url=str(source.url),
            content=content,
        )
    )

    return Setup(
        research_repository=(research_repository),
        run_repository=run_repository,
        plan_repository=plan_repository,
        document_repository=(document_repository),
        application=application,
        aggregate=aggregate,
        run=run,
        source=source,
    )


def build_service(
    setup: Setup,
    extractor: StaticEvidenceExtractor,
    *,
    chunk_size_chars: int = 100,
    overlap_chars: int = 0,
    workload_settings: EvidenceWorkloadSettings | None = None,
    max_concurrency: int = 4,
) -> ResearchEvidenceExtractionService:
    return ResearchEvidenceExtractionService(
        setup.research_repository,
        setup.run_repository,
        setup.plan_repository,
        setup.document_repository,
        extractor,
        chunking_settings=(
            DocumentChunkingSettings(
                chunk_size_chars=chunk_size_chars,
                overlap_chars=overlap_chars,
            )
        ),
        workload_settings=workload_settings,
        max_concurrency=max_concurrency,
    )


def test_verified_candidate_becomes_evidence_with_absolute_locator() -> None:
    async def run_test() -> None:
        setup = await setup_run(content=("0123456789 target 0123456789"))

        extractor = StaticEvidenceExtractor(
            lambda chunk: (
                [
                    EvidenceCandidate(
                        content="target",
                        relevance_score=0.9,
                    )
                ]
                if "target" in chunk.content
                else []
            )
        )

        service = build_service(
            setup,
            extractor,
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert summary == (
            EvidenceExtractionSummary(
                eligible_document_count=1,
                selected_document_count=1,
                available_chunk_count=1,
                scheduled_chunk_count=1,
                skipped_chunk_count=0,
                candidate_count=1,
                verified_count=1,
                rejected_count=0,
                deduplicated_count=0,
                created_count=1,
                reused_count=0,
            )
        )

        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)

        assert aggregate is not None
        assert len(aggregate.evidence) == 1

        evidence = next(iter(aggregate.evidence.values()))

        assert evidence.content == "target"

        assert evidence.locator == "chars:11-17"

    asyncio.run(run_test())


def test_hallucinated_candidate_is_rejected() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="real source text")

        extractor = StaticEvidenceExtractor(
            lambda _chunk: [
                EvidenceCandidate(
                    content=("invented statement"),
                    relevance_score=0.9,
                )
            ]
        )

        service = build_service(
            setup,
            extractor,
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert summary.candidate_count == 1

        assert summary.verified_count == 0

        assert summary.rejected_count == 1

        assert summary.created_count == 0

        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)

        assert aggregate is not None
        assert aggregate.evidence == {}

    asyncio.run(run_test())


def test_overlap_duplicate_is_created_once() -> None:
    async def run_test() -> None:
        setup = await setup_run(content=("A" * 10 + "TARGET" + "B" * 10))

        extractor = StaticEvidenceExtractor(
            lambda chunk: (
                [
                    EvidenceCandidate(
                        content="TARGET",
                        relevance_score=0.8,
                    )
                ]
                if "TARGET" in chunk.content
                else []
            )
        )

        service = build_service(
            setup,
            extractor,
            chunk_size_chars=16,
            overlap_chars=8,
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert summary.verified_count == 2

        assert summary.created_count == 1

        assert summary.deduplicated_count == 1

        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)

        assert aggregate is not None
        assert len(aggregate.evidence) == 1

        evidence = next(iter(aggregate.evidence.values()))

        assert evidence.locator == "chars:10-16"

    asyncio.run(run_test())


def test_existing_evidence_is_reused_idempotently() -> None:
    async def run_test() -> None:
        setup = await setup_run(content=("0123456789 target 0123456789"))

        await setup.application.add_evidence(
            setup.aggregate.task.task_id,
            Evidence(
                source_id=(setup.source.source_id),
                content="target",
                locator="chars:11-17",
                relevance_score=0.7,
            ),
        )

        extractor = StaticEvidenceExtractor(
            lambda chunk: (
                [
                    EvidenceCandidate(
                        content="target",
                        relevance_score=0.9,
                    )
                ]
                if "target" in chunk.content
                else []
            )
        )

        service = build_service(
            setup,
            extractor,
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert summary.created_count == 0

        assert summary.reused_count == 1

        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)

        assert aggregate is not None
        assert len(aggregate.evidence) == 1

    asyncio.run(run_test())


def test_wrong_run_state_is_rejected_before_model_call() -> None:
    async def run_test() -> None:
        setup = await setup_run(
            content="source text",
            target_status=(ResearchRunStatus.EXTRACTING),
        )

        extractor = StaticEvidenceExtractor(lambda _chunk: [])

        service = build_service(
            setup,
            extractor,
        )

        with pytest.raises(
            ResearchEvidenceExtractionError,
            match="verifying",
        ):
            await service.extract_run(
                setup.aggregate.task.task_id,
                setup.run.run_id,
            )

        assert extractor.calls == []

    asyncio.run(run_test())


def test_workload_budget_caps_chunks_before_model_call() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="abcdefghijklmnopqrstuvwxyz")

        extractor = StaticEvidenceExtractor(lambda _chunk: [])

        service = build_service(
            setup,
            extractor,
            chunk_size_chars=4,
            overlap_chars=0,
            workload_settings=(
                EvidenceWorkloadSettings(
                    max_documents=1,
                    max_chunks_per_document=3,
                    max_total_chunks=2,
                )
            ),
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert [chunk.index for chunk in extractor.calls] == [
            0,
            1,
        ]

        assert summary.eligible_document_count == 1

        assert summary.selected_document_count == 1

        assert summary.available_chunk_count == 7

        assert summary.scheduled_chunk_count == 2

        assert summary.skipped_chunk_count == 5

        assert summary.candidate_count == 0

        assert summary.created_count == 0

    asyncio.run(run_test())


class ConcurrencyTrackingEvidenceExtractor:
    """Track simultaneous extractor calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def extract(
        self,
        *,
        task,
        run,
        plan,
        source,
        chunk,
    ):
        del (
            task,
            run,
            plan,
            source,
            chunk,
        )

        self.active += 1

        self.max_active = max(
            self.max_active,
            self.active,
        )

        try:
            await asyncio.sleep(0.01)
            return []

        finally:
            self.active -= 1


def test_bounded_concurrency_caps_simultaneous_model_calls() -> None:
    async def run_test() -> None:
        setup = await setup_run(content=("abcdefghijklmnopqrstuvwx"))

        extractor = ConcurrencyTrackingEvidenceExtractor()

        service = build_service(
            setup,
            extractor,
            chunk_size_chars=4,
            overlap_chars=0,
            workload_settings=(
                EvidenceWorkloadSettings(
                    max_documents=1,
                    max_chunks_per_document=6,
                    max_total_chunks=6,
                )
            ),
            max_concurrency=2,
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )

        assert extractor.max_active == 2

        assert summary.scheduled_chunk_count == 6

        assert summary.candidate_count == 0

    asyncio.run(run_test())


def test_rejects_non_positive_max_concurrency() -> None:
    with pytest.raises(
        ValueError,
        match=("max_concurrency must be at least 1"),
    ):
        ResearchEvidenceExtractionService(
            object(),
            object(),
            object(),
            object(),
            object(),
            max_concurrency=0,
        )
