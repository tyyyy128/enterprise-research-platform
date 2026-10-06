from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from enterprise_research.infrastructure.searching import (
    TavilySearchAdapterError,
    TavilySearchProvider,
    TavilySearchSettings,
)


def create_provider(
    transport: httpx.MockTransport,
) -> tuple[
    TavilySearchProvider,
    httpx.AsyncClient,
]:
    client = httpx.AsyncClient(transport=transport)

    settings = TavilySearchSettings(
        api_key="tvly-secret",
        base_url="https://example.test/",
    )

    return (
        TavilySearchProvider(
            settings,
            client=client,
        ),
        client,
    )


def test_success_maps_request_and_results() -> None:
    async def run() -> None:
        seen: dict[
            str,
            object,
        ] = {}

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen["url"] = str(request.url)

            seen["auth"] = request.headers["Authorization"]

            seen["body"] = json.loads(request.content)

            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": ("LangGraph docs"),
                            "url": ("https://example.com/docs"),
                            "content": ("Durable execution documentation."),
                            "score": 0.91,
                            "published_date": ("2026-10-01"),
                            "id": "result-1",
                            "favicon": ("https://example.com/favicon.ico"),
                        },
                        {
                            "title": "Second",
                            "url": ("https://example.com/second"),
                            "content": "Second result",
                            "score": 0.50,
                        },
                    ],
                    "request_id": "request-1",
                    "response_time": "0.42",
                },
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            results = await provider.search(
                "  durable execution  ",
                max_results=1,
            )

        finally:
            await client.aclose()

        assert seen["url"] == ("https://example.test/search")

        assert seen["auth"] == ("Bearer tvly-secret")

        assert seen["body"] == {
            "query": "durable execution",
            "search_depth": "basic",
            "max_results": 1,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": False,
        }

        assert len(results) == 1

        result = results[0]

        assert result.title == ("LangGraph docs")

        assert str(result.url) == ("https://example.com/docs")

        assert result.snippet == ("Durable execution documentation.")

        assert result.score == 0.91
        assert result.provider == "tavily"

        assert result.metadata["request_id"] == "request-1"

        assert result.metadata["result_id"] == "result-1"

        assert result.metadata["published_date"] == "2026-10-01"

    asyncio.run(run())


def test_empty_results_are_valid() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [],
                },
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            results = await provider.search(
                "durable execution",
                max_results=5,
            )

        finally:
            await client.aclose()

        assert results == []

    asyncio.run(run())


def test_http_error_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                429,
                json={"detail": {"error": ("rate limited")}},
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilySearchAdapterError,
                match="request failed",
            ):
                await provider.search(
                    "query",
                    max_results=5,
                )

        finally:
            await client.aclose()

    asyncio.run(run())


def test_invalid_json_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"not-json",
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilySearchAdapterError,
                match="invalid JSON",
            ):
                await provider.search(
                    "query",
                    max_results=5,
                )

        finally:
            await client.aclose()

    asyncio.run(run())


def test_invalid_result_schema_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "",
                            "url": "not-a-url",
                        }
                    ]
                },
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilySearchAdapterError,
                match=("invalid search response"),
            ):
                await provider.search(
                    "query",
                    max_results=5,
                )

        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    (
        "query",
        "max_results",
    ),
    [
        ("   ", 5),
        ("query", 0),
        ("query", 21),
    ],
)
def test_invalid_inputs_fail_before_http(
    query: str,
    max_results: int,
) -> None:
    async def run() -> None:
        calls = 0

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls

            calls += 1

            return httpx.Response(
                200,
                json={
                    "results": [],
                },
            )

        provider, client = create_provider(httpx.MockTransport(handler))

        try:
            with pytest.raises(ValueError):
                await provider.search(
                    query,
                    max_results=max_results,
                )

        finally:
            await client.aclose()

        assert calls == 0

    asyncio.run(run())


def test_settings_validate_configuration() -> None:
    with pytest.raises(ValueError):
        TavilySearchSettings(api_key=" ")

    with pytest.raises(ValueError):
        TavilySearchSettings(
            api_key="secret",
            timeout_seconds=0,
        )

    with pytest.raises(ValueError):
        TavilySearchSettings(
            api_key="secret",
            search_depth="bogus",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError):
        TavilySearchSettings(
            api_key="secret",
            topic="bogus",  # type: ignore[arg-type]
        )
