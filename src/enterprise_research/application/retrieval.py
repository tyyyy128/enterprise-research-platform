"""Provider-neutral contracts for fetching web source content."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    field_validator,
)


class WebFetchResult(BaseModel):
    """Successful content returned by a web fetching provider."""

    requested_url: HttpUrl
    final_url: HttpUrl

    content: str

    content_type: str = Field(
        default="text/markdown",
        min_length=1,
    )

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(
        cls,
        value: str,
    ) -> str:
        if not value.strip():
            raise ValueError("Fetched content must not be blank.")

        return value

    @field_validator("content_type")
    @classmethod
    def content_type_must_not_be_blank(
        cls,
        value: str,
    ) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError("content_type must not be blank.")

        return normalized


class WebContentFetcher(Protocol):
    """Fetch readable content for one absolute HTTP(S) URL."""

    async def fetch(
        self,
        url: str,
    ) -> WebFetchResult:
        """Return readable content or raise provider-specific error."""
