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


class ResearchEvidenceManifestRow(Base):
    """Fenced receipt for completed evidence-extraction work, including zero hits."""

    __tablename__ = "research_run_evidence_manifests"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_evidence_manifest_fencing"),
        CheckConstraint("eligible_document_count > 0", name="ck_evidence_manifest_documents"),
        CheckConstraint("scheduled_chunk_count > 0", name="ck_evidence_manifest_scheduled"),
        CheckConstraint("completed_chunk_count = scheduled_chunk_count", name="ck_evidence_manifest_completed"),
        CheckConstraint(
            "verified_count >= 0 AND created_count >= 0 AND reused_count >= 0 "
            "AND created_count + reused_count <= verified_count",
            name="ck_evidence_manifest_counters",
        ),
        CheckConstraint("jsonb_typeof(document_hashes) = 'object'", name="ck_evidence_manifest_hash_shape"),
        CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="ck_evidence_manifest_id_shape"),
        CheckConstraint("jsonb_typeof(evidence_hashes) = 'object'", name="ck_evidence_manifest_evidence_shape"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eligible_document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reused_count: Mapped[int] = mapped_column(Integer, nullable=False)
    document_hashes: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_hashes: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchRunClaimGenerationRow(Base):
    """Write-once receipt binding candidate Claims to a fenced Run input snapshot."""

    __tablename__ = "research_run_claim_generations"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_run_claim_generation_fencing"),
        CheckConstraint("available_evidence_count >= 0", name="ck_run_claim_generation_available"),
        CheckConstraint("candidate_count >= 0", name="ck_run_claim_generation_candidate_count"),
        CheckConstraint("length(input_sha256) = 64", name="ck_run_claim_generation_input_hash"),
        CheckConstraint("length(output_sha256) = 64", name="ck_run_claim_generation_output_hash"),
        CheckConstraint("length(btrim(generator_model)) > 0", name="ck_run_claim_generation_model"),
        CheckConstraint("jsonb_typeof(selected_evidence_ids) = 'array'", name="ck_run_claim_generation_evidence"),
        CheckConstraint("jsonb_typeof(claim_ids) = 'array'", name="ck_run_claim_generation_claims"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    available_evidence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    claim_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generator_model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchRunClaimVerificationRow(Base):
    """Write-once Run/Fencing verdicts; Claim status changes are same-transaction."""

    __tablename__ = "research_run_claim_verifications"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_run_claim_verification_fencing"),
        CheckConstraint("candidate_count >= 0", name="ck_run_claim_verification_count"),
        CheckConstraint("length(input_sha256) = 64", name="ck_run_claim_verification_input"),
        CheckConstraint("length(verdicts_sha256) = 64", name="ck_run_claim_verification_output"),
        CheckConstraint("length(generation_input_sha256) = 64", name="ck_run_claim_verification_gen_input"),
        CheckConstraint("length(generation_output_sha256) = 64", name="ck_run_claim_verification_gen_output"),
        CheckConstraint("length(btrim(verifier_model)) > 0", name="ck_run_claim_verification_model"),
        CheckConstraint("jsonb_typeof(claim_ids) = 'array'", name="ck_run_claim_verification_claims"),
        CheckConstraint("jsonb_typeof(verdicts) = 'array'", name="ck_run_claim_verification_verdicts"),
        CheckConstraint("jsonb_typeof(limits) = 'object'", name="ck_run_claim_verification_limits"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verdicts_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    verdicts: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    verifier_model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchRunReportRow(Base):
    """Write-once, run/generation-scoped evidence-backed report snapshot."""

    __tablename__ = "research_run_report_artifacts"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_run_report_fencing"),
        CheckConstraint("length(btrim(title)) > 0", name="ck_run_report_title"),
        CheckConstraint("length(btrim(body)) > 0", name="ck_run_report_body"),
        CheckConstraint("length(body_sha256) = 64", name="ck_run_report_body_hash"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_run_report_payload_hash"),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="ck_run_report_citations"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchRunReportSynthesisRow(Base):
    """3H write-once model outline; verified against live Run facts at terminal commit."""

    __tablename__ = "research_run_report_syntheses"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_report_synthesis_fencing"),
        CheckConstraint("length(input_sha256) = 64", name="ck_report_synthesis_input"),
        CheckConstraint("length(receipt_sha256) = 64", name="ck_report_synthesis_receipt"),
        CheckConstraint("length(btrim(model)) > 0", name="ck_report_synthesis_model"),
        CheckConstraint("jsonb_typeof(plan) = 'object'", name="ck_report_synthesis_plan"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_run_report_artifacts.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
