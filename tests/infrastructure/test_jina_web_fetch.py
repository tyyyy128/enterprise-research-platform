from __future__ import annotations

import asyncio

import httpx
import pytest

from enterprise_research.infrastructure.retrieval import (
    JinaWebContentFetcher,
    JinaWebFetchError,
    JinaWebFetchSettings,
)


def create_fetcher(
    transport: httpx.MockTransport,
    *,
    api_key: str | None = "jina-secret",
    max_content_chars: int = 1_000_000,
) -> tuple[
    JinaWebContentFetcher,
    httpx.AsyncClient,
]:
    client = httpx.AsyncClient(transport=transport)

    settings = JinaWebFetchSettings(
        api_key=api_key,
        base_url="https://reader.test/",
        timeout_seconds=12.5,
        max_content_chars=(max_content_chars),
    )

    return (
        JinaWebContentFetcher(
            settings,
            client=client,
        ),
        client,
    )


def test_wrapped_object_response_maps_request_and_content() -> None:
    async def run() -> None:
        seen: dict[
            str,
            object,
        ] = {}

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen["url"] = str(request.url)

            seen["auth"] = request.headers.get("Authorization")

            seen["accept"] = request.headers.get("Accept")

            seen["respond_with"] = request.headers.get("X-Respond-With")

            seen["timeout"] = request.headers.get("X-Timeout")

            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "status": 20000,
                    "data": {
                        "url": ("https://example.com/final"),
                        "title": "Example",
                        "content": ("# Example\n\nBody"),
                        "timestamp": ("2026-10-06T00:00:00Z"),
                    },
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            result = await fetcher.fetch("https://example.com/start?x=1")

        finally:
            await client.aclose()

        assert seen == {
            "url": ("https://reader.test/https://example.com/start?x=1"),
            "auth": ("Bearer jina-secret"),
            "accept": ("application/json"),
            "respond_with": ("markdown"),
            "timeout": "12.5",
        }

        assert str(result.requested_url) == ("https://example.com/start?x=1")

        assert str(result.final_url) == ("https://example.com/final")

        assert result.content == ("# Example\n\nBody")

        assert result.content_type == "text/markdown"

        assert result.metadata["provider"] == "jina"

        assert result.metadata["jina_code"] == 200

        assert result.metadata["jina_status"] == 20000

        assert result.metadata["title"] == "Example"

    asyncio.run(run())


def test_wrapped_string_response_is_supported() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "status": 20000,
                    "data": "# Body",
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            result = await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

        assert result.content == "# Body"

        assert str(result.final_url) == ("https://example.com/a")

    asyncio.run(run())


def test_direct_object_response_is_supported_and_key_is_optional() -> None:
    async def run() -> None:
        seen_auth: list[str | None] = []

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization"))

            return httpx.Response(
                200,
                json={
                    "url": ("https://example.com/final"),
                    "markdown": ("Direct body"),
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            api_key=None,
        )

        try:
            result = await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

        assert seen_auth == [None]

        assert result.content == "Direct body"

    asyncio.run(run())


def test_http_error_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "detail": "blocked",
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                JinaWebFetchError,
                match="HTTP 403",
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
                JinaWebFetchError,
                match="invalid JSON",
            ):
                await fetcher.fetch("https://example.com/a")

        finally:
            await client.aclose()

    asyncio.run(run())


def test_empty_content_is_rejected() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "content": "  ",
                    }
                },
            )

        fetcher, client = create_fetcher(httpx.MockTransport(handler))

        try:
            with pytest.raises(
                JinaWebFetchError,
                match="empty web content",
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
                    "data": {
                        "content": "abcdef",
                    }
                },
            )

        fetcher, client = create_fetcher(
            httpx.MockTransport(handler),
            max_content_chars=5,
        )

        try:
            with pytest.raises(
                JinaWebFetchError,
                match="max_content_chars",
            ):
                await fetcher.fetch("https://example.com/a")

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
                json={
                    "data": "body",
                },
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


def test_settings_validate_timeout_and_content_limit() -> None:
    with pytest.raises(
        ValueError,
        match="timeout_seconds",
    ):
        JinaWebFetchSettings(timeout_seconds=0)

    with pytest.raises(
        ValueError,
        match="timeout_seconds",
    ):
        JinaWebFetchSettings(timeout_seconds=181)

    with pytest.raises(
        ValueError,
        match="max_content_chars",
    ):
        JinaWebFetchSettings(max_content_chars=0)
