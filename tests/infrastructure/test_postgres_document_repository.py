from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import delete

from enterprise_research.domain import (
    ResearchAggregate,
    RetrievalStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresRetrievedDocumentRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)

RUN_DB_TESTS = os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason=("Set ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 to run PostgreSQL integration tests."),
)


def test_postgres_document_repository_round_trip_and_replace() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()

        engine = build_async_engine(settings)

        session_factory = build_session_factory(engine)

        aggregate_repository = PostgresResearchRepository(session_factory)

        document_repository = PostgresRetrievedDocumentRepository(session_factory)

        aggregate = ResearchAggregate.create("Research retrieval")

        source = aggregate.add_source(
            Source(
                source_type=SourceType.WEB,
                title="Example",
                url="https://example.com/a",
            )
        )

        try:
            await aggregate_repository.save(aggregate)

            successful = RetrievedDocument.succeeded(
                source_id=source.source_id,
                requested_url=("https://example.com/a"),
                final_url=("https://example.com/final"),
                content="# Retrieved",
                metadata={
                    "provider": "fake",
                },
            )

            await document_repository.save(successful)

            loaded = await document_repository.get_for_source(source.source_id)

            assert loaded == successful

            failed = RetrievedDocument.failed(
                source_id=source.source_id,
                requested_url=("https://example.com/a"),
                error="timeout",
            )

            await document_repository.save(failed)

            replaced = await document_repository.get_for_source(source.source_id)

            assert replaced is not None

            assert replaced.status == RetrievalStatus.FAILED

            assert replaced.error == "timeout"
            assert replaced.content is None

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())


def test_aggregate_resave_preserves_retrieved_document() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()

        engine = build_async_engine(settings)

        session_factory = build_session_factory(engine)

        aggregate_repository = PostgresResearchRepository(session_factory)

        document_repository = PostgresRetrievedDocumentRepository(session_factory)

        aggregate = ResearchAggregate.create("Research stable source rows")

        source = aggregate.add_source(
            Source(
                source_type=SourceType.WEB,
                title="Original title",
                url="https://example.com/a",
            )
        )

        document = RetrievedDocument.succeeded(
            source_id=source.source_id,
            requested_url=("https://example.com/a"),
            final_url=("https://example.com/a"),
            content="Stable body",
        )

        try:
            await aggregate_repository.save(aggregate)

            await document_repository.save(document)

            aggregate.sources[source.source_id].title = "Updated title"

            await aggregate_repository.save(aggregate)

            await aggregate_repository.save(aggregate)

            preserved = await document_repository.get_for_source(source.source_id)

            assert preserved == document

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())
