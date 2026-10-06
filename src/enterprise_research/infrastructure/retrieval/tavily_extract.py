"""Tavily Extract adapter for provider-neutral web content retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
)

from enterprise_research.application.retrieval import (
    WebFetchResult,
)


class TavilyExtractWebFetchError(RuntimeError):
    """Raised when Tavily Extract cannot return usable web content."""


@dataclass(frozen=True, slots=True)
class TavilyExtractSettings:
    """Connection and safety settings for Tavily Extract."""

    api_key: str

    base_url: str = "https://api.tavily.com"

    extract_depth: Literal[
        "basic",
        "advanced",
    ] = "basic"

    extract_timeout_seconds: float = 30.0
    http_timeout_seconds: float = 60.0

    max_content_chars: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("api_key must not be blank.")

        if not self.base_url.strip():
            raise ValueError("base_url must not be blank.")

        if self.extract_depth not in {
            "basic",
            "advanced",
        }:
            raise ValueError("Unsupported Tavily extract_depth.")

        if not (1 <= self.extract_timeout_seconds <= 60):
            raise ValueError("extract_timeout_seconds must be between 1 and 60.")

        if self.http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be greater than zero.")

        if self.max_content_chars < 1:
            raise ValueError("max_content_chars must be at least 1.")

    @property
    def extract_url(self) -> str:
        """Return Tavily Extract endpoint."""
        return f"{self.base_url.rstrip('/')}/extract"


class _ExtractResultPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: HttpUrl

    raw_content: str = Field(min_length=1)

    favicon: HttpUrl | None = None


class _FailedResultPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: HttpUrl

    error: str = Field(min_length=1)


class _ExtractResponsePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_ExtractResultPayload] = Field(default_factory=list)

    failed_results: list[_FailedResultPayload] = Field(default_factory=list)

    response_time: float | None = None
    request_id: str | None = None

    usage: (
        dict[
            str,
            Any,
        ]
        | None
    ) = None


class TavilyExtractWebContentFetcher:
    """Fetch one web page as Markdown through Tavily Extract."""

    def __init__(
        self,
        settings: TavilyExtractSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def fetch(
        self,
        url: str,
    ) -> WebFetchResult:
        """Extract one HTTP(S) page as readable Markdown."""
        normalized_url = _validate_target_url(url)

        if self._client is not None:
            return await self._fetch_with_client(
                self._client,
                normalized_url,
            )

        async with httpx.AsyncClient(
            timeout=(self._settings.http_timeout_seconds),
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
        try:
            response = await client.post(
                self._settings.extract_url,
                headers={
                    "Authorization": (f"Bearer {self._settings.api_key}"),
                    "Content-Type": ("application/json"),
                },
                json={
                    "urls": url,
                    "extract_depth": (self._settings.extract_depth),
                    "include_images": False,
                    "include_favicon": True,
                    "format": "markdown",
                    "timeout": (self._settings.extract_timeout_seconds),
                    "include_usage": True,
                },
            )

            response.raise_for_status()

        except httpx.HTTPStatusError as exc:
            raise TavilyExtractWebFetchError(f"Tavily Extract returned HTTP {exc.response.status_code}.") from exc

        except httpx.RequestError as exc:
            raise TavilyExtractWebFetchError("Tavily Extract request failed.") from exc

        payload = self._parse_response(response)

        if payload.results:
            if len(payload.results) != 1:
                raise TavilyExtractWebFetchError("Tavily Extract returned an unexpected number of results.")

            result = payload.results[0]

            content = result.raw_content

            if not content.strip():
                raise TavilyExtractWebFetchError("Tavily Extract returned empty web content.")

            if len(content) > self._settings.max_content_chars:
                raise TavilyExtractWebFetchError("Tavily Extract content exceeds max_content_chars.")

            metadata: dict[
                str,
                Any,
            ] = {
                "provider": ("tavily-extract"),
                "extract_depth": (self._settings.extract_depth),
            }

            if payload.request_id is not None:
                metadata["request_id"] = payload.request_id

            if payload.response_time is not None:
                metadata["response_time"] = payload.response_time

            if payload.usage is not None:
                metadata["usage"] = payload.usage

            if result.favicon is not None:
                metadata["favicon"] = str(result.favicon)

            try:
                return WebFetchResult(
                    requested_url=url,
                    final_url=result.url,
                    content=content,
                    content_type=("text/markdown"),
                    metadata=metadata,
                )

            except ValidationError as exc:
                raise (TavilyExtractWebFetchError("Tavily Extract returned invalid web content.")) from exc

        if payload.failed_results:
            failure = payload.failed_results[0]

            raise TavilyExtractWebFetchError(f"Tavily Extract failed for {failure.url}: {failure.error}")

        raise TavilyExtractWebFetchError("Tavily Extract returned neither results nor failed_results.")

    @staticmethod
    def _parse_response(
        response: httpx.Response,
    ) -> _ExtractResponsePayload:
        try:
            raw: Any = response.json()

        except ValueError as exc:
            raise TavilyExtractWebFetchError("Tavily Extract returned invalid JSON.") from exc

        try:
            return _ExtractResponsePayload.model_validate(raw)

        except ValidationError as exc:
            details = "; ".join(
                (f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}") for issue in exc.errors()
            )

            raise TavilyExtractWebFetchError(f"Tavily Extract returned an invalid response: {details}") from exc


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
