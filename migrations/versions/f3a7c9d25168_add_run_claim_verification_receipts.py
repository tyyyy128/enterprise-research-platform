"""Add fenced, write-once Run Claim verification receipts.

Revision ID: f3a7c9d25168
Revises: b6e9f4c0a21d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f3a7c9d25168"
down_revision: str | Sequence[str] | None = "b6e9f4c0a21d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_claim_verifications",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("generation_input_sha256", sa.String(64), nullable=False),
        sa.Column("generation_output_sha256", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("verdicts_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("claim_ids", JSONB(), nullable=False),
        sa.Column("verdicts", JSONB(), nullable=False),
        sa.Column("limits", JSONB(), nullable=False),
        sa.Column("verifier_model", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("fencing_token > 0", name="ck_run_claim_verification_fencing"),
        sa.CheckConstraint("candidate_count >= 0", name="ck_run_claim_verification_count"),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_run_claim_verification_input"),
        sa.CheckConstraint("length(verdicts_sha256) = 64", name="ck_run_claim_verification_output"),
        sa.CheckConstraint("length(generation_input_sha256) = 64", name="ck_run_claim_verification_gen_input"),
        sa.CheckConstraint("length(generation_output_sha256) = 64", name="ck_run_claim_verification_gen_output"),
        sa.CheckConstraint("length(btrim(verifier_model)) > 0", name="ck_run_claim_verification_model"),
        sa.CheckConstraint("jsonb_typeof(claim_ids) = 'array'", name="ck_run_claim_verification_claims"),
        sa.CheckConstraint("jsonb_typeof(verdicts) = 'array'", name="ck_run_claim_verification_verdicts"),
        sa.CheckConstraint("jsonb_typeof(limits) = 'object'", name="ck_run_claim_verification_limits"),
    )


def downgrade() -> None:
    op.drop_table("research_run_claim_verifications")
