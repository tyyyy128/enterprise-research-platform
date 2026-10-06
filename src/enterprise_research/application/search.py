"""Search contracts shared by research search adapters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SearchResult(BaseModel):
    """Provider-neutral search result returned to the application layer."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1)
    url: HttpUrl
    snippet: str = ""
    score: float | None = Field(default=None, ge=0.0)
    provider: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchProvider(Protocol):
    """Search the public web without exposing provider-specific APIs."""

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> Sequence[SearchResult]:
        """Return ranked results for one focused search query."""
