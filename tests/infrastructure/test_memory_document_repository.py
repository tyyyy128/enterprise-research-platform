from __future__ import annotations

import asyncio
from uuid import uuid4

from enterprise_research.domain import (
    RetrievalStatus,
    RetrievedDocument,
)
from enterprise_research.infrastructure import (
    InMemoryRetrievedDocumentRepository,
)


def test_unknown_source_has_no_document() -> None:
    async def run() -> None:
        repository = InMemoryRetrievedDocumentRepository()

        assert await repository.get_for_source(uuid4()) is None

    asyncio.run(run())


def test_document_repository_returns_deep_copy() -> None:
    async def run() -> None:
        repository = InMemoryRetrievedDocumentRepository()

        source_id = uuid4()

        document = RetrievedDocument.succeeded(
            source_id=source_id,
            requested_url=("https://example.com/start"),
            final_url=("https://example.com/final"),
            content="# Retrieved",
            metadata={
                "nested": {
                    "value": 1,
                }
            },
        )

        await repository.save(document)

        loaded = await repository.get_for_source(source_id)

        assert loaded is not None

        loaded.metadata["nested"]["value"] = 2

        loaded_again = await repository.get_for_source(source_id)

        assert loaded_again is not None

        assert loaded_again.metadata["nested"]["value"] == 1

    asyncio.run(run())


def test_save_replaces_latest_retrieval_outcome() -> None:
    async def run() -> None:
        repository = InMemoryRetrievedDocumentRepository()

        source_id = uuid4()

        await repository.save(
            RetrievedDocument.succeeded(
                source_id=source_id,
                requested_url=("https://example.com/a"),
                final_url=("https://example.com/a"),
                content="body",
            )
        )

        await repository.save(
            RetrievedDocument.failed(
                source_id=source_id,
                requested_url=("https://example.com/a"),
                error="timeout",
            )
        )

        loaded = await repository.get_for_source(source_id)

        assert loaded is not None

        assert loaded.status == RetrievalStatus.FAILED

        assert loaded.error == "timeout"
        assert loaded.content is None

    asyncio.run(run())
