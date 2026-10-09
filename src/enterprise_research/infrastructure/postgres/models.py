"""SQLAlchemy ORM models for Enterprise Research."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)


class Base(DeclarativeBase):
    """Base class for Enterprise Research ORM models."""


class ResearchTaskRow(Base):
    """Persisted research task."""

    __tablename__ = "research_tasks"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    query: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ResearchSourceRow(Base):
    """Persisted research source."""

    __tablename__ = "research_sources"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    # "metadata" is reserved by SQLAlchemy declarative models,
    # so the Python attribute has a different name.
    source_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )


class ResearchSourceDocumentRow(Base):
    """Latest retrieval outcome for one research source."""

    __tablename__ = "research_source_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_research_source_documents_status",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_sources.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )

    requested_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    final_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    content: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    content_type: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    document_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )


class ResearchEvidenceRow(Base):
    """Persisted research evidence."""

    __tablename__ = "research_evidence"
    __table_args__ = (
        CheckConstraint(
            "relevance_score >= 0.0 AND relevance_score <= 1.0",
            name="ck_research_evidence_relevance_score",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_sources.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    locator: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    relevance_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ResearchClaimRow(Base):
    """Persisted research claim."""

    __tablename__ = "research_claims"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_research_claims_confidence",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )


class ClaimEvidenceRow(Base):
    """Ordered evidence references supporting a claim."""

    __tablename__ = "claim_evidence"
    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "position",
            name="uq_claim_evidence_position",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_claims.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_evidence.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class ResearchReportRow(Base):
    """Persisted final research report."""

    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            name="uq_research_reports_task_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ReportClaimRow(Base):
    """Ordered claims included in a research report."""

    __tablename__ = "report_claims"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "position",
            name="uq_report_claims_position",
        ),
    )

    report_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_reports.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_claims.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class ResearchRunRow(Base):
    """Persisted execution of one research task."""

    __tablename__ = "research_runs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    lease_owner_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_fencing_token: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
    )


class ResearchStageAttemptRow(Base):
    """One fenced stage attempt, including entries left in doubt by a crash."""

    __tablename__ = "research_run_stage_attempts"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('planning', 'searching', 'extracting', 'verifying', 'writing')",
            name="ck_research_run_stage_attempts_stage",
        ),
        CheckConstraint(
            "outcome IN ('started', 'completed', 'failed')",
            name="ck_research_run_stage_attempts_outcome",
        ),
        CheckConstraint("fencing_token > 0", name="ck_research_run_stage_attempts_fence"),
        CheckConstraint(
            "(outcome = 'started' AND finished_at IS NULL AND error IS NULL) OR "
            "(outcome = 'completed' AND finished_at IS NOT NULL AND error IS NULL) OR "
            "(outcome = 'failed' AND finished_at IS NOT NULL "
            "AND error IS NOT NULL AND length(btrim(error)) > 0)",
            name="ck_research_run_stage_attempts_finished_shape",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_research_run_stage_attempts_time_order",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ResearchPlanRow(Base):
    """Persisted structured research plan."""

    __tablename__ = "research_plans"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_runs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        unique=True,
    )
    objective: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ResearchPlanQuestionRow(Base):
    """One ordered question inside a research plan."""

    __tablename__ = "research_plan_questions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    plan_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "research_plans.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    rationale: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
