"""Provider-neutral contracts for extracting evidence from documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import (
    BaseModel,
    Field,
    field_validator,
)

from enterprise_research.domain import (
    ResearchPlan,
    ResearchRun,
    ResearchTask,
    Source,
)


class EvidenceCandidate(BaseModel):
    """One candidate excerpt proposed as research evidence."""

    content: str

    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
    )

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(
        cls,
        value: str,
    ) -> str:
        if not value.strip():
            raise ValueError("Evidence candidate content must not be blank.")

        return value


@dataclass(
    frozen=True,
    slots=True,
)
class DocumentChunk:
    """Deterministic character range from one retrieved document."""

    index: int
    start_offset: int
    end_offset: int
    content: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("index must be non-negative.")

        if self.start_offset < 0:
            raise ValueError("start_offset must be non-negative.")

        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset.")

        if len(self.content) != (self.end_offset - self.start_offset):
            raise ValueError("Chunk content length does not match its offsets.")


class EvidenceExtractor(Protocol):
    """Extract verbatim evidence candidates from one document chunk."""

    async def extract(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
        plan: ResearchPlan,
        source: Source,
        chunk: DocumentChunk,
    ) -> list[EvidenceCandidate]:
        """Return evidence excerpts relevant to the research objective."""
