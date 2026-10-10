"""Add run/fencing-scoped evidence extraction completion receipts.

Revision ID: c4e7b0a6d9f2
Revises: 7c3a1e9d4b62
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c4e7b0a6d9f2"
down_revision: str | Sequence[str] | None = "7c3a1e9d4b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_evidence_manifests",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("eligible_document_count", sa.Integer(), nullable=False),
        sa.Column("scheduled_chunk_count", sa.Integer(), nullable=False),
        sa.Column("completed_chunk_count", sa.Integer(), nullable=False),
        sa.Column("verified_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("reused_count", sa.Integer(), nullable=False),
        sa.Column("document_hashes", JSONB(), nullable=False),
        sa.Column("evidence_ids", JSONB(), nullable=False),
        sa.Column("evidence_hashes", JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("fencing_token > 0", name="ck_evidence_manifest_fencing"),
        sa.CheckConstraint("eligible_document_count > 0", name="ck_evidence_manifest_documents"),
        sa.CheckConstraint("scheduled_chunk_count > 0", name="ck_evidence_manifest_scheduled"),
        sa.CheckConstraint("completed_chunk_count = scheduled_chunk_count", name="ck_evidence_manifest_completed"),
        sa.CheckConstraint(
            "verified_count >= 0 AND created_count >= 0 AND reused_count >= 0 "
            "AND created_count + reused_count <= verified_count",
            name="ck_evidence_manifest_counters",
        ),
        sa.CheckConstraint("jsonb_typeof(document_hashes) = 'object'", name="ck_evidence_manifest_hash_shape"),
        sa.CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="ck_evidence_manifest_id_shape"),
        sa.CheckConstraint("jsonb_typeof(evidence_hashes) = 'object'", name="ck_evidence_manifest_evidence_shape"),
    )


def downgrade() -> None:
    op.drop_table("research_run_evidence_manifests")
