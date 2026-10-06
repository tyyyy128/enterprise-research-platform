"""Domain model for retrieved source documents."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, model_validator

from enterprise_research.domain.models import utc_now


class RetrievalStatus(StrEnum):
    """Outcome of retrieving one research source."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetrievedDocument(BaseModel):
    """Persistable retrieval outcome for one research source."""

    source_id: UUID
    requested_url: HttpUrl
    final_url: HttpUrl | None = None
    status: RetrievalStatus

    content: str | None = None
    content_type: str | None = None
    content_hash: str | None = None

    fetched_at: datetime = Field(default_factory=utc_now)

    error: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def char_count(self) -> int:
        """Return the number of characters in fetched content."""
        return len(self.content or "")

    @classmethod
    def succeeded(
        cls,
        *,
        source_id: UUID,
        requested_url: str | HttpUrl,
        final_url: str | HttpUrl,
        content: str,
        content_type: str = "text/markdown",
        metadata: dict[str, Any] | None = None,
    ) -> RetrievedDocument:
        """Create a successful retrieval outcome with SHA-256."""
        return cls(
            source_id=source_id,
            requested_url=requested_url,
            final_url=final_url,
            status=RetrievalStatus.SUCCEEDED,
            content=content,
            content_type=content_type,
            content_hash=sha256(content.encode("utf-8")).hexdigest(),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def failed(
        cls,
        *,
        source_id: UUID,
        requested_url: str | HttpUrl,
        error: str,
        final_url: str | HttpUrl | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RetrievedDocument:
        """Create a failed retrieval outcome without content."""
        return cls(
            source_id=source_id,
            requested_url=requested_url,
            final_url=final_url,
            status=RetrievalStatus.FAILED,
            error=error.strip(),
            metadata=dict(metadata or {}),
        )

    @model_validator(mode="after")
    def validate_outcome(
        self,
    ) -> RetrievedDocument:
        """Keep success and failure states internally consistent."""
        if self.status == RetrievalStatus.SUCCEEDED:
            if self.content is None or not self.content.strip():
                raise ValueError("Successful retrieval requires non-blank content.")

            if self.final_url is None:
                raise ValueError("Successful retrieval requires final_url.")

            if self.content_type is None or not self.content_type.strip():
                raise ValueError("Successful retrieval requires content_type.")

            if self.content_hash is None or not self.content_hash.strip():
                raise ValueError("Successful retrieval requires content_hash.")

            expected_hash = sha256(self.content.encode("utf-8")).hexdigest()

            if self.content_hash != expected_hash:
                raise ValueError("Successful retrieval content_hash does not match content.")

            if self.error is not None:
                raise ValueError("Successful retrieval cannot contain an error.")

            return self

        if self.error is None or not self.error.strip():
            raise ValueError("Failed retrieval requires a non-blank error.")

        if self.content is not None:
            raise ValueError("Failed retrieval cannot contain content.")

        if self.content_hash is not None:
            raise ValueError("Failed retrieval cannot contain content_hash.")

        return self
