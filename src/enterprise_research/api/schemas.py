"""HTTP request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from enterprise_research.application.report_writing import ResearchRunReportArtifact
from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    ResearchAggregate,
    ResearchReport,
    ResearchRun,
    ResearchRunStatus,
    ResearchTaskStatus,
    Source,
    SourceType,
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


class CreateSourceRequest(BaseModel):
    """Request for adding a source to a research task."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_type: SourceType
    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceResponse(BaseModel):
    """Stable API representation of a research source."""

    source_id: UUID
    source_type: SourceType
    title: str
    url: HttpUrl | None
    retrieved_at: datetime
    content_hash: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_domain(cls, source: Source) -> SourceResponse:
        return cls(
            source_id=source.source_id,
            source_type=source.source_type,
            title=source.title,
            url=source.url,
            retrieved_at=source.retrieved_at,
            content_hash=source.content_hash,
            metadata=source.metadata,
        )


class CreateEvidenceRequest(BaseModel):
    """Request for adding evidence extracted from a source."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_id: UUID
    content: str = Field(min_length=1, max_length=100000)
    locator: str | None = Field(default=None, max_length=2000)
    relevance_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
    )


class EvidenceResponse(BaseModel):
    """Stable API representation of research evidence."""

    evidence_id: UUID
    source_id: UUID
    content: str
    locator: str | None
    relevance_score: float
    created_at: datetime

    @classmethod
    def from_domain(cls, evidence: Evidence) -> EvidenceResponse:
        return cls(
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_id,
            content=evidence.content,
            locator=evidence.locator,
            relevance_score=evidence.relevance_score,
            created_at=evidence.created_at,
        )


class CreateClaimRequest(BaseModel):
    """Request for creating an unverified research claim."""

    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=10000)
    evidence_ids: list[UUID] = Field(default_factory=list)


class SupportClaimRequest(BaseModel):
    """Request for supporting a claim with evidence."""

    evidence_ids: list[UUID] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ClaimResponse(BaseModel):
    """Stable API representation of a research claim."""

    claim_id: UUID
    text: str
    status: ClaimStatus
    evidence_ids: list[UUID]
    confidence: float

    @classmethod
    def from_domain(cls, claim: Claim) -> ClaimResponse:
        return cls(
            claim_id=claim.claim_id,
            text=claim.text,
            status=claim.status,
            evidence_ids=claim.evidence_ids,
            confidence=claim.confidence,
        )


class EvidenceLineageItemResponse(BaseModel):
    """One evidence-to-source link in claim lineage."""

    evidence: EvidenceResponse
    source: SourceResponse


class ClaimLineageResponse(BaseModel):
    """Trace a claim back to evidence and original sources."""

    claim_id: UUID
    items: list[EvidenceLineageItemResponse]

    @classmethod
    def from_domain(
        cls,
        claim_id: UUID,
        lineage: list[tuple[Evidence, Source]],
    ) -> ClaimLineageResponse:
        return cls(
            claim_id=claim_id,
            items=[
                EvidenceLineageItemResponse(
                    evidence=EvidenceResponse.from_domain(evidence),
                    source=SourceResponse.from_domain(source),
                )
                for evidence, source in lineage
            ],
        )


class CreateReportRequest(BaseModel):
    """Request for generating a research report."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=1000)
    summary: str = Field(default="", max_length=50000)
    claim_ids: list[UUID] | None = None


class ResearchReportResponse(BaseModel):
    """Stable API representation of a research report."""

    report_id: UUID
    task_id: UUID
    title: str
    summary: str
    claim_ids: list[UUID]
    generated_at: datetime

    @classmethod
    def from_domain(
        cls,
        report: ResearchReport,
    ) -> ResearchReportResponse:
        return cls(
            report_id=report.report_id,
            task_id=report.task_id,
            title=report.title,
            summary=report.summary,
            claim_ids=report.claim_ids,
            generated_at=report.generated_at,
        )


class ResearchRunResponse(BaseModel):
    """Stable API representation of one research execution run."""

    run_id: UUID
    task_id: UUID
    status: ResearchRunStatus

    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    error: str | None

    @classmethod
    def from_domain(
        cls,
        run: ResearchRun,
    ) -> ResearchRunResponse:
        return cls(
            run_id=run.run_id,
            task_id=run.task_id,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            error=run.error,
        )


class ResearchRunReportResponse(BaseModel):
    """Immutable, run-owned final report; different from the task-level report."""

    task_id: UUID
    run_id: UUID
    fencing_token: int
    title: str
    body: str
    citations: list[dict[str, Any]]
    body_sha256: str
    payload_sha256: str
    created_at: datetime
    synthesis_model: str | None = None
    synthesis_sha256: str | None = None

    @classmethod
    def from_artifact(cls, artifact: ResearchRunReportArtifact) -> ResearchRunReportResponse:
        return cls(
            task_id=artifact.task_id,
            run_id=artifact.run_id,
            fencing_token=artifact.fencing_token,
            title=artifact.title,
            body=artifact.body,
            citations=list(artifact.citations),
            body_sha256=artifact.body_sha256,
            payload_sha256=artifact.payload_sha256,
            created_at=artifact.created_at,
            synthesis_model=artifact.synthesis_model,
            synthesis_sha256=artifact.synthesis_sha256,
        )
