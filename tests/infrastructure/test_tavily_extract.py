from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from enterprise_research.infrastructure.retrieval import (
    TavilyExtractSettings,
    TavilyExtractWebContentFetcher,
    TavilyExtractWebFetchError,
)


def create_fetcher(
    transport: httpx.MockTransport,
    *,
    max_content_chars: int = 1_000_000,
    oversize_policy: str = "reject",
) -> tuple[
    TavilyExtractWebContentFetcher,
    httpx.AsyncClient,
]:
    client = httpx.AsyncClient(transport=transport)

    settings = TavilyExtractSettings(
        api_key="tvly-secret",
        base_url="https://api.test/",
        extract_depth="basic",
        extract_timeout_seconds=30.0,
        http_timeout_seconds=60.0,
        max_content_chars=(max_content_chars),
        oversize_policy=oversize_policy,
    )

    return (
        TavilyExtractWebContentFetcher(
            settings,
            client=client,
        ),
        client,
    )


def test_success_maps_request_and_result() -> None:
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
                            "url": ("https://example.com/final"),
                            "raw_content": ("# Retrieved\n\nBody"),
                            "favicon": ("https://example.com/favicon.ico"),
                        }
                    ],
                    "failed_results": [],
                    "response_time": 0.01,
                    "request_id": "request-1",
                    "usage": {
                        "credits": 0,
                    },
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            result = await fetcher.fetch("https://example.com/start")

        finally:
            await client.aclose()

        assert seen["url"] == ("https://api.test/extract")

        assert seen["auth"] == ("Bearer tvly-secret")

        assert seen["body"] == {
            "urls": ("https://example.com/start"),
            "extract_depth": "basic",
            "include_images": False,
            "include_favicon": True,
            "format": "markdown",
            "timeout": 30.0,
            "include_usage": True,
        }

        assert str(result.requested_url) == ("https://example.com/start")

        assert str(result.final_url) == ("https://example.com/final")

        assert result.content == ("# Retrieved\n\nBody")

        assert result.content_type == "text/markdown"

        assert result.metadata["provider"] == "tavily-extract"

        assert result.metadata["request_id"] == "request-1"

        assert result.metadata["response_time"] == 0.01

        assert result.metadata["usage"] == {
            "credits": 0,
        }

    asyncio.run(run())


def test_http_200_failed_result_is_raised() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "failed_results": [
                        {
                            "url": ("https://example.com/a"),
                            "error": ("Failed to retrieve content"),
                        }
                    ],
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match=("Failed to retrieve content"),
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_http_error_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    "detail": "rate limited",
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match="HTTP 429",
            ):
                await fetcher.fetch("https://example.com/a")

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

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match="invalid JSON",
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_invalid_schema_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "not-a-url",
                            "raw_content": "body",
                        }
                    ],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match="invalid response",
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_unexpected_multiple_results_are_rejected() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": ("https://example.com/a"),
                            "raw_content": "A",
                        },
                        {
                            "url": ("https://example.com/b"),
                            "raw_content": "B",
                        },
                    ],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match=("unexpected number of results"),
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_content_size_limit_is_enforced() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": ("https://example.com/a"),
                            "raw_content": ("abcdef"),
                        }
                    ],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            max_content_chars=5,
        )

        try:
            with pytest.raises(
                TavilyExtractWebFetchError,
                match="max_content_chars",
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_opt_in_truncation_preserves_exact_source_prefix_and_provenance() -> None:
    async def run() -> None:
        raw = "# Revenue\n\n" + "内容" * 16 + " FINANCIAL DATA"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com/report", "raw_content": raw}],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            max_content_chars=18,
            oversize_policy="truncate",
        )
        try:
            result = await fetcher.fetch("https://example.com/report")
        finally:
            await client.aclose()

        assert result.content == raw[:18]
        assert len(result.content) == 18
        assert result.metadata["retrieval_truncated"] is True
        assert result.metadata["retrieval_original_content_chars"] == len(raw)
        assert result.metadata["retrieval_stored_content_chars"] == 18
        assert result.metadata["retrieval_truncation_strategy"] == "prefix"
        assert result.metadata["provider"] == "tavily-extract"

    asyncio.run(run())


def test_truncation_keeps_complete_short_document_unmarked() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com/report", "raw_content": "Revenue: 123"}],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            max_content_chars=12,
            oversize_policy="truncate",
        )
        try:
            result = await fetcher.fetch("https://example.com/report")
        finally:
            await client.aclose()

        assert result.content == "Revenue: 123"
        assert "retrieval_truncated" not in result.metadata

    asyncio.run(run())


def test_truncation_rejects_blank_prefix_even_when_full_page_is_not_blank() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com/report", "raw_content": "    real data"}],
                    "failed_results": [],
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            max_content_chars=3,
            oversize_policy="truncate",
        )
        try:
            with pytest.raises(TavilyExtractWebFetchError, match="truncated content is blank"):
                await fetcher.fetch("https://example.com/report")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_invalid_target_url_fails_before_http() -> None:
    async def run() -> None:
        calls = 0

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal calls

            calls += 1

            return httpx.Response(
                200,
                json={},
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                ValueError,
                match="absolute HTTP",
            ):
                await fetcher.fetch("file:///etc/passwd")

        finally:
            await client.aclose()

        assert calls == 0

    asyncio.run(run())


def test_settings_validate_configuration() -> None:
    with pytest.raises(
        ValueError,
        match="api_key",
    ):
        TavilyExtractSettings(api_key=" ")

    with pytest.raises(
        ValueError,
        match="extract_timeout_seconds",
    ):
        TavilyExtractSettings(
            api_key="secret",
            extract_timeout_seconds=0,
        )

    with pytest.raises(
        ValueError,
        match="extract_timeout_seconds",
    ):
        TavilyExtractSettings(
            api_key="secret",
            extract_timeout_seconds=61,
        )

    with pytest.raises(
        ValueError,
        match="http_timeout_seconds",
    ):
        TavilyExtractSettings(
            api_key="secret",
            http_timeout_seconds=0,
        )

    with pytest.raises(
        ValueError,
        match="max_content_chars",
    ):
        TavilyExtractSettings(
            api_key="secret",
            max_content_chars=0,
        )

    with pytest.raises(ValueError, match="oversize_policy"):
        TavilyExtractSettings(api_key="secret", oversize_policy="ignore")
