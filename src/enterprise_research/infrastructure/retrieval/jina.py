"""Jina Reader adapter for provider-neutral web content retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from enterprise_research.application.retrieval import (
    WebFetchResult,
)


class JinaWebFetchError(RuntimeError):
    """Raised when Jina Reader cannot return usable web content."""


@dataclass(frozen=True, slots=True)
class JinaWebFetchSettings:
    """Connection and safety settings for Jina Reader."""

    api_key: str | None = None
    base_url: str = "https://r.jina.ai"
    timeout_seconds: float = 30.0
    max_content_chars: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("base_url must not be blank.")

        if not 0 < self.timeout_seconds <= 180:
            raise ValueError("timeout_seconds must be greater than zero and at most 180.")

        if self.max_content_chars < 1:
            raise ValueError("max_content_chars must be at least 1.")

    def reader_url(
        self,
        target_url: str,
    ) -> str:
        """Return Reader endpoint for one target URL."""
        return f"{self.base_url.rstrip('/')}/{target_url}"


class JinaWebContentFetcher:
    """Fetch LLM-friendly Markdown through Jina Reader."""

    def __init__(
        self,
        settings: JinaWebFetchSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def fetch(
        self,
        url: str,
    ) -> WebFetchResult:
        """Fetch one HTTP(S) resource as Markdown."""
        normalized_url = _validate_target_url(url)

        if self._client is not None:
            return await self._fetch_with_client(
                self._client,
                normalized_url,
            )

        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
        ) as client:
            return await self._fetch_with_client(
                client,
                normalized_url,
            )

    async def _fetch_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> WebFetchResult:
        headers = {
            "Accept": "application/json",
            "X-Respond-With": "markdown",
            "X-Timeout": _format_timeout(self._settings.timeout_seconds),
        }

        api_key = (self._settings.api_key or "").strip()

        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await client.get(
                self._settings.reader_url(url),
                headers=headers,
            )

            response.raise_for_status()

        except httpx.HTTPStatusError as exc:
            raise JinaWebFetchError(f"Jina Reader returned HTTP {exc.response.status_code}.") from exc

        except httpx.RequestError as exc:
            raise JinaWebFetchError("Jina Reader request failed.") from exc

        (
            content,
            final_url,
            metadata,
        ) = self._parse_response(
            response,
            requested_url=url,
        )

        if len(content) > self._settings.max_content_chars:
            raise JinaWebFetchError("Jina Reader content exceeds max_content_chars.")

        try:
            return WebFetchResult(
                requested_url=url,
                final_url=final_url,
                content=content,
                content_type="text/markdown",
                metadata=metadata,
            )

        except ValidationError as exc:
            raise JinaWebFetchError("Jina Reader returned invalid web content.") from exc

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        *,
        requested_url: str,
    ) -> tuple[
        str,
        str,
        dict[str, Any],
    ]:
        try:
            raw: Any = response.json()

        except ValueError as exc:
            raise JinaWebFetchError("Jina Reader returned invalid JSON.") from exc

        metadata: dict[str, Any] = {
            "provider": "jina",
        }

        payload: Any = raw

        if isinstance(raw, dict) and "data" in raw:
            payload = raw["data"]

            if isinstance(
                raw.get("code"),
                int,
            ):
                metadata["jina_code"] = raw["code"]

            if isinstance(
                raw.get("status"),
                int,
            ):
                metadata["jina_status"] = raw["status"]

        if isinstance(
            payload,
            str,
        ):
            content = payload
            final_url = requested_url

        elif isinstance(
            payload,
            dict,
        ):
            content = payload.get("content")

            if not isinstance(
                content,
                str,
            ):
                content = payload.get("markdown")

            final_url_raw = payload.get(
                "url",
                requested_url,
            )

            final_url = (
                final_url_raw
                if isinstance(
                    final_url_raw,
                    str,
                )
                else requested_url
            )

            for key in (
                "title",
                "description",
                "publishedTime",
                "timestamp",
            ):
                value = payload.get(key)

                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                        bool,
                    ),
                ):
                    metadata[key] = value

        else:
            raise JinaWebFetchError("Jina Reader returned an unsupported response payload.")

        if (
            not isinstance(
                content,
                str,
            )
            or not content.strip()
        ):
            raise JinaWebFetchError("Jina Reader returned empty web content.")

        _validate_target_url(final_url)

        return (
            content,
            final_url,
            metadata,
        )


def _validate_target_url(
    url: str,
) -> str:
    normalized = url.strip()

    parsed = urlsplit(normalized)

    if (
        parsed.scheme
        not in {
            "http",
            "https",
        }
        or not parsed.netloc
    ):
        raise ValueError("url must be an absolute HTTP(S) URL.")

    return normalized


def _format_timeout(
    value: float,
) -> str:
    numeric = float(value)

    if numeric.is_integer():
        return str(int(numeric))

    return str(numeric)
