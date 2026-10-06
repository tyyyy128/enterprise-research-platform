"""Tavily adapter for provider-neutral research web search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
)

from enterprise_research.application.search import (
    SearchResult,
)


class TavilySearchAdapterError(RuntimeError):
    """Raised when Tavily cannot return a usable search response."""


@dataclass(frozen=True, slots=True)
class TavilySearchSettings:
    """Connection and request defaults for Tavily Search."""

    api_key: str
    base_url: str = "https://api.tavily.com"

    search_depth: Literal[
        "advanced",
        "basic",
        "fast",
        "ultra-fast",
    ] = "basic"

    topic: Literal[
        "general",
        "news",
        "finance",
    ] = "general"

    timeout_seconds: float = 30.0

    include_published_date: bool = False

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("api_key must not be blank.")

        if not self.base_url.strip():
            raise ValueError("base_url must not be blank.")

        if self.search_depth not in {
            "advanced",
            "basic",
            "fast",
            "ultra-fast",
        }:
            raise ValueError("Unsupported Tavily search_depth.")

        if self.topic not in {
            "general",
            "news",
            "finance",
        }:
            raise ValueError("Unsupported Tavily topic.")

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

    @property
    def search_url(self) -> str:
        """Return the Tavily Search endpoint."""
        return f"{self.base_url.rstrip('/')}/search"


class _TavilyResultPayload(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
    )

    title: str = Field(min_length=1)
    url: HttpUrl

    content: str = ""

    score: float | None = Field(
        default=None,
        ge=0.0,
    )

    published_date: str | None = None
    favicon: HttpUrl | None = None
    id: str | None = None


class _TavilyResponsePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_TavilyResultPayload] = Field(default_factory=list)

    request_id: str | None = None
    response_time: float | str | None = None


class TavilySearchProvider:
    """Search the public web through Tavily's HTTP API."""

    def __init__(
        self,
        settings: TavilySearchSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        """Return provider-neutral results for one focused query."""
        normalized_query = query.strip()

        if not normalized_query:
            raise ValueError("query must not be blank.")

        # Tavily itself permits 0..20, but our research
        # pipeline requires an actual positive search budget.
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20.")

        if self._client is not None:
            return await self._search_with_client(
                self._client,
                query=normalized_query,
                max_results=max_results,
            )

        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
        ) as client:
            return await self._search_with_client(
                client,
                query=normalized_query,
                max_results=max_results,
            )

    async def _search_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        request_body = {
            "query": query,
            "search_depth": (self._settings.search_depth),
            "max_results": max_results,
            "topic": self._settings.topic,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": (self._settings.include_published_date),
        }

        try:
            response = await client.post(
                self._settings.search_url,
                headers={
                    "Authorization": (f"Bearer {self._settings.api_key}"),
                    "Content-Type": ("application/json"),
                },
                json=request_body,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise TavilySearchAdapterError("Tavily search request failed.") from exc

        payload = self._parse_response(response)

        return [
            SearchResult(
                title=result.title,
                url=result.url,
                snippet=result.content,
                score=result.score,
                provider="tavily",
                metadata=_result_metadata(
                    result,
                    payload,
                ),
            )
            for result in payload.results[:max_results]
        ]

    @staticmethod
    def _parse_response(
        response: httpx.Response,
    ) -> _TavilyResponsePayload:
        try:
            raw: Any = response.json()

        except ValueError as exc:
            raise TavilySearchAdapterError("Tavily returned invalid JSON.") from exc

        try:
            return _TavilyResponsePayload.model_validate(raw)

        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in exc.errors()
            )

            raise TavilySearchAdapterError(f"Tavily returned an invalid search response: {details}") from exc


def _result_metadata(
    result: _TavilyResultPayload,
    response: _TavilyResponsePayload,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}

    if result.published_date is not None:
        metadata["published_date"] = result.published_date

    if result.favicon is not None:
        metadata["favicon"] = str(result.favicon)

    if result.id is not None:
        metadata["result_id"] = result.id

    if response.request_id is not None:
        metadata["request_id"] = response.request_id

    if response.response_time is not None:
        metadata["response_time"] = response.response_time

    return metadata
