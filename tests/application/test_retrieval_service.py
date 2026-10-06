from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchRetrievalError,
    ResearchRetrievalService,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
    WebFetchResult,
)
from enterprise_research.domain import (
    ResearchAggregate,
    ResearchRun,
    ResearchRunStatus,
    RetrievalStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)


class RecordingFetcher:
    def __init__(
        self,
        responses: dict[
            str,
            WebFetchResult | Exception,
        ],
    ) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def fetch(
        self,
        url: str,
    ) -> WebFetchResult:
        self.calls.append(url)

        response = self.responses[url]

        if isinstance(
            response,
            Exception,
        ):
            raise response

        return response


async def setup_extracting_run(
    *,
    source_count: int = 2,
) -> tuple[
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
    ResearchAggregate,
    ResearchRun,
    list[Source],
]:
    research_repository = InMemoryResearchRepository()

    run_repository = InMemoryResearchRunRepository()

    document_repository = InMemoryRetrievedDocumentRepository()

    aggregate = ResearchAggregate.create("Research durable execution")

    sources: list[Source] = []

    for position in range(source_count):
        source = Source(
            source_type=SourceType.WEB,
            title=(f"Source {position}"),
            url=(f"https://example.com/{position}"),
        )

        aggregate.add_source(source)

        sources.append(source)

    await research_repository.save(aggregate)

    run = ResearchRun.create(aggregate.task.task_id)

    for status in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
    ):
        run.advance(status)

    await run_repository.save(run)

    return (
        research_repository,
        run_repository,
        document_repository,
        aggregate,
        run,
        sources,
    )


def successful_result(
    url: str,
    *,
    content: str = "# Retrieved",
) -> WebFetchResult:
    return WebFetchResult(
        requested_url=url,
        final_url=url,
        content=content,
        metadata={
            "provider": "fake",
        },
    )


def test_partial_failure_is_persisted_and_run_can_continue() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            sources,
        ) = await setup_extracting_run()

        first_url = str(sources[0].url)

        second_url = str(sources[1].url)

        fetcher = RecordingFetcher(
            {
                first_url: (successful_result(first_url)),
                second_url: (RuntimeError("blocked")),
            }
        )

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        summary = await service.retrieve_run(
            aggregate.task.task_id,
            run.run_id,
        )

        assert summary.eligible_count == 2

        assert summary.attempted_count == 2

        assert summary.succeeded_count == 1

        assert summary.failed_count == 1

        assert summary.reused_count == 0

        assert summary.usable_count == 1

        assert summary.coverage_ratio == 0.5

        successful = await document_repository.get_for_source(sources[0].source_id)

        failed = await document_repository.get_for_source(sources[1].source_id)

        assert successful is not None
        assert failed is not None

        assert successful.status == RetrievalStatus.SUCCEEDED

        assert failed.status == RetrievalStatus.FAILED

        assert failed.error == "blocked"

        assert failed.metadata["error_type"] == "RuntimeError"

    asyncio.run(run_test())


def test_all_failures_are_saved_before_service_raises() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            sources,
        ) = await setup_extracting_run()

        fetcher = RecordingFetcher({str(source.url): (RuntimeError("unavailable")) for source in sources})

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        with pytest.raises(
            ResearchRetrievalError,
            match="no usable documents",
        ):
            await service.retrieve_run(
                aggregate.task.task_id,
                run.run_id,
            )

        for source in sources:
            document = await document_repository.get_for_source(source.source_id)

            assert document is not None

            assert document.status == RetrievalStatus.FAILED

    asyncio.run(run_test())


def test_existing_success_is_reused_without_fetching_again() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            sources,
        ) = await setup_extracting_run(source_count=1)

        source = sources[0]

        await document_repository.save(
            RetrievedDocument.succeeded(
                source_id=(source.source_id),
                requested_url=(source.url),
                final_url=(source.url),
                content=("Previously fetched"),
            )
        )

        fetcher = RecordingFetcher({})

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        summary = await service.retrieve_run(
            aggregate.task.task_id,
            run.run_id,
        )

        assert fetcher.calls == []

        assert summary.attempted_count == 0

        assert summary.reused_count == 1

        assert summary.coverage_ratio == 1.0

    asyncio.run(run_test())


def test_existing_failure_is_retried_and_replaced() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            sources,
        ) = await setup_extracting_run(source_count=1)

        source = sources[0]
        url = str(source.url)

        await document_repository.save(
            RetrievedDocument.failed(
                source_id=(source.source_id),
                requested_url=url,
                error="old failure",
            )
        )

        fetcher = RecordingFetcher(
            {
                url: successful_result(
                    url,
                    content="new body",
                )
            }
        )

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        summary = await service.retrieve_run(
            aggregate.task.task_id,
            run.run_id,
        )

        assert summary.succeeded_count == 1

        loaded = await document_repository.get_for_source(source.source_id)

        assert loaded is not None

        assert loaded.status == RetrievalStatus.SUCCEEDED

        assert loaded.content == "new body"

    asyncio.run(run_test())


def test_changed_source_url_refetches_existing_success() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            sources,
        ) = await setup_extracting_run(source_count=1)

        source = sources[0]

        current_url = str(source.url)

        await document_repository.save(
            RetrievedDocument.succeeded(
                source_id=(source.source_id),
                requested_url=("https://old.example.com/a"),
                final_url=("https://old.example.com/a"),
                content="old body",
            )
        )

        fetcher = RecordingFetcher(
            {
                current_url: (
                    successful_result(
                        current_url,
                        content="new body",
                    )
                )
            }
        )

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        summary = await service.retrieve_run(
            aggregate.task.task_id,
            run.run_id,
        )

        assert summary.attempted_count == 1

        assert fetcher.calls == [current_url]

        loaded = await document_repository.get_for_source(source.source_id)

        assert loaded is not None

        assert loaded.content == "new body"

    asyncio.run(run_test())


def test_wrong_run_state_is_rejected_before_fetching() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            _sources,
        ) = await setup_extracting_run(source_count=1)

        run.status = ResearchRunStatus.SEARCHING

        await run_repository.save(run)

        fetcher = RecordingFetcher({})

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        with pytest.raises(
            ResearchRetrievalError,
            match="extracting",
        ):
            await service.retrieve_run(
                aggregate.task.task_id,
                run.run_id,
            )

        assert fetcher.calls == []

    asyncio.run(run_test())


def test_task_without_web_sources_is_rejected() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            _sources,
        ) = await setup_extracting_run(source_count=0)

        aggregate.add_source(
            Source(
                source_type=(SourceType.INTERNAL_DOCUMENT),
                title="Internal document",
            )
        )

        await research_repository.save(aggregate)

        fetcher = RecordingFetcher({})

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            fetcher,
        )

        with pytest.raises(
            ResearchRetrievalError,
            match="no retrievable web sources",
        ):
            await service.retrieve_run(
                aggregate.task.task_id,
                run.run_id,
            )

        assert fetcher.calls == []

    asyncio.run(run_test())


def test_missing_task_is_rejected() -> None:
    async def run_test() -> None:
        service = ResearchRetrievalService(
            InMemoryResearchRepository(),
            InMemoryResearchRunRepository(),
            InMemoryRetrievedDocumentRepository(),
            RecordingFetcher({}),
        )

        with pytest.raises(ResearchTaskNotFoundError):
            await service.retrieve_run(
                uuid4(),
                uuid4(),
            )

    asyncio.run(run_test())


def test_run_from_another_task_is_rejected() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            document_repository,
            aggregate,
            run,
            _sources,
        ) = await setup_extracting_run(source_count=1)

        run.task_id = uuid4()

        await run_repository.save(run)

        service = ResearchRetrievalService(
            research_repository,
            run_repository,
            document_repository,
            RecordingFetcher({}),
        )

        with pytest.raises(ResearchRunNotFoundError):
            await service.retrieve_run(
                aggregate.task.task_id,
                run.run_id,
            )

    asyncio.run(run_test())
