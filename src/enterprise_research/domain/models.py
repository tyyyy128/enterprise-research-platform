"""Core domain models for enterprise research."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, model_validator


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class ResearchTaskStatus(StrEnum):
    CREATED = "created"
    RESEARCHING = "researching"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceType(StrEnum):
    WEB = "web"
    INTERNAL_DOCUMENT = "internal_document"
    DATABASE = "database"
    API = "api"


class ClaimStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTED = "conflicted"


class Source(BaseModel):
    """A source from which research evidence can be extracted."""

    source_id: UUID = Field(default_factory=uuid4)
    source_type: SourceType
    title: str = Field(min_length=1)
    url: HttpUrl | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    content_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A concrete piece of evidence extracted from one source."""

    evidence_id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    content: str = Field(min_length=1)
    locator: str | None = None
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class Claim(BaseModel):
    """A research claim backed by zero or more evidence records."""

    claim_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1)
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    evidence_ids: list[UUID] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_supported_claim(self) -> Claim:
        """A supported claim must reference at least one evidence record."""
        if self.status == ClaimStatus.SUPPORTED and not self.evidence_ids:
            raise ValueError("A supported claim must reference at least one evidence record.")
        return self


class ResearchReport(BaseModel):
    """The final report assembled from verified claims."""

    report_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    title: str = Field(min_length=1)
    summary: str = ""
    claim_ids: list[UUID] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class ResearchTask(BaseModel):
    """The aggregate root for one research request."""

    task_id: UUID = Field(default_factory=uuid4)
    query: str = Field(min_length=1)
    status: ResearchTaskStatus = ResearchTaskStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    source_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    claim_ids: list[UUID] = Field(default_factory=list)
    report_id: UUID | None = None
