"""HTTP request and response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from enterprise_research.domain import (
    ResearchAggregate,
    ResearchTaskStatus,
)


class CreateResearchTaskRequest(BaseModel):
    """Request for creating a research task."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)


class ResearchTaskResponse(BaseModel):
    """Stable API representation of a research task."""

    task_id: UUID
    query: str
    status: ResearchTaskStatus
    created_at: datetime
    updated_at: datetime

    source_count: int
    evidence_count: int
    claim_count: int

    report_id: UUID | None

    @classmethod
    def from_aggregate(
        cls,
        aggregate: ResearchAggregate,
    ) -> ResearchTaskResponse:
        task = aggregate.task

        return cls(
            task_id=task.task_id,
            query=task.query,
            status=task.status,
            created_at=task.created_at,
            updated_at=task.updated_at,
            source_count=len(aggregate.sources),
            evidence_count=len(aggregate.evidence),
            claim_count=len(aggregate.claims),
            report_id=task.report_id,
        )
