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


def test_focused_selection_preserves_mechanical_evidence_offsets() -> None:
    async def run_test() -> None:
        prefix = "X" * 40
        quote = "Revenue was 100 dollars"
        document = prefix + quote + " in 2024."
        setup = await setup_run(content=document)
        extractor = StaticEvidenceExtractor(
            lambda chunk: [EvidenceCandidate(content=quote, relevance_score=0.95)] if quote in chunk.content else []
        )
        service = build_service(
            setup,
            extractor,
            chunk_size_chars=40,
            overlap_chars=0,
            workload_settings=EvidenceWorkloadSettings(
                max_documents=1,
                max_chunks_per_document=1,
                max_total_chunks=1,
                selection_mode="focus",
                focus_terms=("revenue",),
            ),
        )

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
        )
        assert summary.scheduled_chunk_count == 1
        assert summary.verified_count == 1
        assert len(extractor.calls) == 1
        assert extractor.calls[0].index == 1
        assert extractor.calls[0].start_offset == 40

        persisted = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert persisted is not None
        evidence = next(iter(persisted.evidence.values()))
        start = document.index(quote)
        assert evidence.locator == f"chars:{start}-{start + len(quote)}"
        assert document[start : start + len(quote)] == evidence.content

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


def test_progress_snapshots_cover_zero_to_complete_with_counts() -> None:
    from enterprise_research.application.evidence_progress import EvidenceExtractionProgress

    async def run_test() -> None:
        setup = await setup_run(content="AAAABBBBCCCC")

        def candidates(chunk: DocumentChunk) -> list[EvidenceCandidate]:
            if chunk.index == 0:
                return [EvidenceCandidate(content="AAAA", relevance_score=0.9)]
            if chunk.index == 1:
                return [EvidenceCandidate(content="not present", relevance_score=0.5)]
            return [
                EvidenceCandidate(content="CCCC", relevance_score=0.7),
                EvidenceCandidate(content="CCCC", relevance_score=0.8),
            ]

        extractor = StaticEvidenceExtractor(candidates)
        service = build_service(setup, extractor, chunk_size_chars=4, max_concurrency=1)
        events: list[EvidenceExtractionProgress] = []

        async def on_progress(progress: EvidenceExtractionProgress) -> None:
            events.append(progress)

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
            progress_callback=on_progress,
        )

        assert [event.completed_chunk_count for event in events] == [0, 1, 2, 3]
        assert [event.completion_ratio for event in events] == [0.0, 1 / 3, 2 / 3, 1.0]
        assert all(event.scheduled_chunk_count == 3 for event in events)
        assert all(event.remaining_chunk_count == 3 - i for i, event in enumerate(events))
        assert [event.candidate_count for event in events] == [0, 1, 2, 4]
        assert [event.verified_count for event in events] == [0, 1, 1, 3]
        assert [event.rejected_count for event in events] == [0, 0, 1, 1]
        assert [event.created_count for event in events] == [0, 1, 1, 2]
        assert [event.deduplicated_count for event in events] == [0, 0, 0, 1]
        assert (summary.candidate_count, summary.verified_count, summary.created_count) == (4, 3, 2)
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert len(aggregate.evidence) == 2

    asyncio.run(run_test())


def test_progress_is_emitted_before_slow_chunk_finishes() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="AAAABBBB")
        release_slow = asyncio.Event()
        first_progress_seen = asyncio.Event()

        class DelayedExtractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                if chunk.index == 0:
                    await release_slow.wait()
                    return [EvidenceCandidate(content="AAAA", relevance_score=0.7)]
                return [EvidenceCandidate(content="BBBB", relevance_score=0.9)]

        events = []

        async def on_progress(progress):
            events.append(progress)
            if progress.completed_chunk_count == 1:
                first_progress_seen.set()

        service = build_service(setup, DelayedExtractor(), chunk_size_chars=4, max_concurrency=2)
        runner = asyncio.create_task(
            service.extract_run(
                setup.aggregate.task.task_id,
                setup.run.run_id,
                progress_callback=on_progress,
            )
        )

        try:
            await asyncio.wait_for(first_progress_seen.wait(), timeout=1)
            assert not runner.done()
            assert [progress.completed_chunk_count for progress in events] == [0, 1]
            assert events[1].created_count == 1
        finally:
            release_slow.set()

        summary = await asyncio.wait_for(runner, timeout=1)
        assert [progress.completed_chunk_count for progress in events] == [0, 1, 2]
        assert summary.created_count == 2

    asyncio.run(run_test())


def test_out_of_order_duplicate_keeps_earliest_planned_evidence() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="A" * 10 + "TARGET" + "B" * 10)
        release_first = asyncio.Event()
        events = []

        class OutOfOrderExtractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                if chunk.index == 0:
                    await release_first.wait()
                return [EvidenceCandidate(content="TARGET", relevance_score=0.9 if chunk.index == 0 else 0.2)]

        async def on_progress(progress):
            events.append(progress)
            if progress.completed_chunk_count == 1:
                assert progress.created_count == 1
                release_first.set()

        service = build_service(
            setup,
            OutOfOrderExtractor(),
            chunk_size_chars=16,
            overlap_chars=8,
            workload_settings=EvidenceWorkloadSettings(max_documents=1, max_chunks_per_document=2, max_total_chunks=2),
            max_concurrency=2,
        )
        summary = await asyncio.wait_for(
            service.extract_run(
                setup.aggregate.task.task_id,
                setup.run.run_id,
                progress_callback=on_progress,
            ),
            timeout=1,
        )

        assert summary.created_count == 1
        assert summary.deduplicated_count == 1
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        evidence = next(iter(aggregate.evidence.values()))
        assert evidence.relevance_score == 0.9
        assert evidence.locator == "chars:10-16"
        assert [event.completed_chunk_count for event in events] == [0, 1, 2]

    asyncio.run(run_test())


def test_initial_callback_exception_prevents_model_calls() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="AAAA")
        extractor = StaticEvidenceExtractor(lambda chunk: [])
        service = build_service(setup, extractor, chunk_size_chars=4)

        async def fail_on_init(progress):
            assert progress.completed_chunk_count == 0
            raise RuntimeError("sink down")

        with pytest.raises(RuntimeError, match="sink down"):
            await service.extract_run(
                setup.aggregate.task.task_id,
                setup.run.run_id,
                progress_callback=fail_on_init,
            )
        assert extractor.calls == []

    asyncio.run(run_test())


def test_callback_failure_cancels_pending_workers_without_partial_evidence() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="AAAABBBB")
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()

        class Extractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                if chunk.index == 0:
                    await slow_started.wait()
                    return [EvidenceCandidate(content="AAAA", relevance_score=0.9)]
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise

        seen = []

        async def fail_after_one(progress):
            seen.append(progress.completed_chunk_count)
            if progress.completed_chunk_count == 1:
                raise RuntimeError("progress publish failed")

        service = build_service(setup, Extractor(), chunk_size_chars=4, max_concurrency=2)
        with pytest.raises(RuntimeError, match="progress publish failed"):
            await asyncio.wait_for(
                service.extract_run(
                    setup.aggregate.task.task_id,
                    setup.run.run_id,
                    progress_callback=fail_after_one,
                ),
                timeout=1,
            )
        assert seen == [0, 1]
        assert slow_cancelled.is_set()
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert aggregate.evidence == {}

    asyncio.run(run_test())


def test_extractor_failure_cancels_other_workers_without_partial_evidence() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="AAAABBBB")
        waiting = asyncio.Event()
        cancelled = asyncio.Event()
        events = []

        class Extractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                if chunk.index == 0:
                    waiting.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                await waiting.wait()
                raise ValueError("LLM rejected request")

        async def on_progress(progress):
            events.append(progress.completed_chunk_count)

        service = build_service(setup, Extractor(), chunk_size_chars=4, max_concurrency=2)
        with pytest.raises(ValueError, match="LLM rejected request"):
            await asyncio.wait_for(
                service.extract_run(
                    setup.aggregate.task.task_id,
                    setup.run.run_id,
                    progress_callback=on_progress,
                ),
                timeout=1,
            )
        assert cancelled.is_set()
        assert events == [0]
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert aggregate.evidence == {}

    asyncio.run(run_test())


def test_parent_cancellation_cancels_workers_without_partial_evidence() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="AAAABBBB")
        both_started = asyncio.Event()
        cancelled = []
        started = []

        class Extractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                started.append(chunk.index)
                if len(started) == 2:
                    both_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(chunk.index)
                    raise

        service = build_service(setup, Extractor(), chunk_size_chars=4, max_concurrency=2)
        runner = asyncio.create_task(service.extract_run(setup.aggregate.task.task_id, setup.run.run_id))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert sorted(cancelled) == [0, 1]
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert aggregate.evidence == {}

    asyncio.run(run_test())


def test_failed_empty_retrieval_does_not_start_work_or_emit_progress() -> None:
    async def run_test() -> None:
        setup = await setup_run(content="Temporary valid content.")

        await setup.document_repository.save(
            RetrievedDocument.failed(
                source_id=setup.source.source_id,
                requested_url=str(setup.source.url),
                error="Fetched document was empty",
            )
        )

        extractor = StaticEvidenceExtractor(lambda chunk: [])
        events = []

        async def on_progress(progress):
            events.append(progress)

        service = build_service(setup, extractor)

        with pytest.raises(
            ResearchEvidenceExtractionError,
            match="no successfully retrieved",
        ):
            await service.extract_run(
                setup.aggregate.task.task_id,
                setup.run.run_id,
                progress_callback=on_progress,
            )

        assert events == []
        assert extractor.calls == []

    asyncio.run(run_test())


def test_zero_scheduled_work_emits_only_complete_zero_progress(monkeypatch) -> None:
    from types import SimpleNamespace

    import enterprise_research.application.evidence_extraction_service as extraction_module

    async def run_test() -> None:
        setup = await setup_run(content="AAAA")
        extractor = StaticEvidenceExtractor(lambda chunk: [])
        events = []
        service = build_service(setup, extractor)
        empty_plan = SimpleNamespace(
            eligible_document_count=1,
            selected_document_count=1,
            available_chunk_count=0,
            scheduled_chunk_count=0,
            skipped_chunk_count=0,
            items=(),
        )
        monkeypatch.setattr(extraction_module, "plan_evidence_workload", lambda *args: empty_plan)

        async def on_progress(progress):
            events.append(progress)

        summary = await service.extract_run(
            setup.aggregate.task.task_id,
            setup.run.run_id,
            progress_callback=on_progress,
        )
        assert summary.scheduled_chunk_count == 0
        assert extractor.calls == []
        assert len(events) == 1
        assert events[0].completed_chunk_count == 0
        assert events[0].completion_ratio == 1.0

    asyncio.run(run_test())


def test_all_three_chunk_completion_orders_produce_same_dedup_result() -> None:
    from itertools import permutations

    async def run_once(order: tuple[int, ...]) -> None:
        setup = await setup_run(content="A" * 10 + "TARGET" + "B" * 10)
        gates = [asyncio.Event() for _ in range(3)]
        all_started = asyncio.Event()
        started: set[int] = set()
        completed_queue = asyncio.Queue()

        class Extractor:
            async def extract(self, *, chunk, **kwargs):
                del kwargs
                started.add(chunk.index)
                if len(started) == 3:
                    all_started.set()
                await gates[chunk.index].wait()
                if chunk.index == 2:
                    return [EvidenceCandidate(content="hallucination", relevance_score=0.1)]
                return [EvidenceCandidate(content="TARGET", relevance_score=0.9 if chunk.index == 0 else 0.2)]

        async def on_progress(progress):
            await completed_queue.put(progress.completed_chunk_count)

        service = build_service(
            setup,
            Extractor(),
            chunk_size_chars=16,
            overlap_chars=8,
            workload_settings=EvidenceWorkloadSettings(max_documents=1, max_chunks_per_document=3, max_total_chunks=3),
            max_concurrency=3,
        )
        run = asyncio.create_task(
            service.extract_run(setup.aggregate.task.task_id, setup.run.run_id, progress_callback=on_progress)
        )
        assert await asyncio.wait_for(completed_queue.get(), 1) == 0
        await asyncio.wait_for(all_started.wait(), 1)
        for completed, index in enumerate(order, start=1):
            gates[index].set()
            assert await asyncio.wait_for(completed_queue.get(), 1) == completed
        summary = await asyncio.wait_for(run, 1)
        assert (summary.candidate_count, summary.verified_count, summary.rejected_count) == (3, 2, 1)
        assert (summary.created_count, summary.deduplicated_count) == (1, 1)
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        evidence = next(iter(aggregate.evidence.values()))
        assert evidence.content == "TARGET"
        assert evidence.relevance_score == 0.9
        assert evidence.locator == "chars:10-16"

    for order in permutations(range(3)):
        asyncio.run(run_once(order))


def test_financial_precision_extracts_deep_original_quote_with_absolute_offsets() -> None:
    async def run_test() -> None:
        # The full page is persisted (not just a 12K prefix). Rank an intact
        # later chunk without modifying its provenance or increasing LLM calls.
        table = (
            "INCOME STATEMENTS\n(In millions)\nYear Ended June 30 | 2024 | 2023 | 2022\n"
            "Product | 64,773 | 64,699 | 72,732\n"
            "Total revenue | 245,122 | 211,915 | 198,270\n"
        )
        exact_quote = table.strip()
        full_text = "Management summary: 2024 revenue exceeds $245 billion.\n".ljust(13_000, ".") + table
        setup = await setup_run(content=full_text)
        extractor = StaticEvidenceExtractor(
            lambda chunk: (
                [EvidenceCandidate(content=exact_quote, relevance_score=0.99)] if exact_quote in chunk.content else []
            )
        )
        service = build_service(
            setup,
            extractor,
            chunk_size_chars=1000,
            workload_settings=EvidenceWorkloadSettings(
                max_documents=1,
                max_chunks_per_document=1,
                max_total_chunks=1,
                selection_mode="financial_precision",
                focus_terms=("2024", "revenue"),
                preferred_domains=("example.com",),
            ),
        )
        summary = await service.extract_run(setup.aggregate.task.task_id, setup.run.run_id)
        assert (summary.scheduled_chunk_count, summary.verified_count, summary.created_count) == (1, 1, 1)
        assert len(extractor.calls) == 1
        assert extractor.calls[0].start_offset >= 13_000
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        evidence = next(iter(aggregate.evidence.values()))
        pos = full_text.index(exact_quote)
        assert evidence.locator == f"chars:{pos}-{pos + len(exact_quote)}"
        assert evidence.content == full_text[pos : pos + len(exact_quote)]
        assert "(In millions)" in evidence.content
        assert "2024 | 2023 | 2022" in evidence.content

    asyncio.run(run_test())


@pytest.mark.parametrize("truncated", [False, True])
def test_financial_precision_rejects_missing_table_before_calling_model(truncated: bool) -> None:
    async def run_test() -> None:
        setup = await setup_run(content="2024 annual revenue was over $245 billion.")
        if truncated:
            document = RetrievedDocument.succeeded(
                source_id=setup.source.source_id,
                requested_url=str(setup.source.url),
                final_url=str(setup.source.url),
                content="2024 annual revenue was over $245 billion.",
                metadata={"retrieval_truncated": True, "retrieval_original_content_chars": 350_000},
            )
            await setup.document_repository.save(document)
        extractor = StaticEvidenceExtractor(lambda chunk: [EvidenceCandidate(content=chunk.content, relevance_score=1)])
        service = build_service(
            setup,
            extractor,
            workload_settings=EvidenceWorkloadSettings(
                selection_mode="financial_precision",
                focus_terms=("2024", "revenue"),
                max_documents=1,
                max_chunks_per_document=1,
                max_total_chunks=1,
            ),
        )
        with pytest.raises(
            ResearchEvidenceExtractionError,
            match=r"No same-chunk Total revenue table.*first column",
        ) as exc:
            await service.extract_run(setup.aggregate.task.task_id, setup.run.run_id)
        assert ("truncated" in str(exc.value)) is truncated
        assert extractor.calls == []
        aggregate = await setup.research_repository.get(setup.aggregate.task.task_id)
        assert aggregate.evidence == {}

    asyncio.run(run_test())
