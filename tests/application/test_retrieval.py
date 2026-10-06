from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from enterprise_research.application import (
    RetrievedDocumentRepository,
    WebContentFetcher,
    WebFetchResult,
)
from enterprise_research.domain import (
    RetrievedDocument,
)


def test_fetch_result_preserves_content_and_redirect() -> None:
    result = WebFetchResult(
        requested_url=("https://example.com/start"),
        final_url=("https://example.com/final"),
        content="  body  ",
        content_type=" text/markdown ",
        metadata={
            "status_code": 200,
        },
    )

    assert result.content == "  body  "

    assert result.content_type == "text/markdown"

    assert str(result.final_url) == "https://example.com/final"

    assert result.metadata == {
        "status_code": 200,
    }


def test_fetch_result_rejects_blank_content() -> None:
    with pytest.raises(
        ValidationError,
        match="must not be blank",
    ):
        WebFetchResult(
            requested_url=("https://example.com/start"),
            final_url=("https://example.com/final"),
            content="\n \t",
        )


def test_fetch_result_rejects_blank_content_type() -> None:
    with pytest.raises(
        ValidationError,
        match="content_type",
    ):
        WebFetchResult(
            requested_url=("https://example.com/start"),
            final_url=("https://example.com/final"),
            content="body",
            content_type="   ",
        )


def test_fetcher_and_repository_contracts_compose() -> None:
    class StaticFetcher:
        async def fetch(
            self,
            url: str,
        ) -> WebFetchResult:
            return WebFetchResult(
                requested_url=url,
                final_url=("https://example.com/final"),
                content="# Retrieved",
            )

    class MemoryRepository:
        def __init__(self) -> None:
            self.items: dict[
                UUID,
                RetrievedDocument,
            ] = {}

        async def save(
            self,
            document: RetrievedDocument,
        ) -> None:
            self.items[document.source_id] = document

        async def get_for_source(
            self,
            source_id: UUID,
        ) -> RetrievedDocument | None:
            return self.items.get(source_id)

    async def run() -> None:
        fetcher: WebContentFetcher = StaticFetcher()

        repository: RetrievedDocumentRepository = MemoryRepository()

        source_id = uuid4()

        fetched = await fetcher.fetch("https://example.com/start")

        document = RetrievedDocument.succeeded(
            source_id=source_id,
            requested_url=(fetched.requested_url),
            final_url=(fetched.final_url),
            content=fetched.content,
            content_type=(fetched.content_type),
            metadata=fetched.metadata,
        )

        await repository.save(document)

        loaded = await repository.get_for_source(source_id)

        assert loaded == document

    asyncio.run(run())
