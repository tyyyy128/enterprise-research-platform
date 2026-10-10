"""Write-once Run/Fencing-scoped receipts for unverified claim proposals.

Revision ID: b6e9f4c0a21d
Revises: ea7c2f19b540
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b6e9f4c0a21d"
down_revision: str | Sequence[str] | None = "ea7c2f19b540"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_claim_generations",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=False),
        sa.Column("available_evidence_count", sa.Integer(), nullable=False),
        sa.Column("selected_evidence_ids", JSONB(), nullable=False),
        sa.Column("claim_ids", JSONB(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("generator_model", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("fencing_token > 0", name="ck_run_claim_generation_fencing"),
        sa.CheckConstraint("available_evidence_count >= 0", name="ck_run_claim_generation_available"),
        sa.CheckConstraint("candidate_count >= 0", name="ck_run_claim_generation_candidate_count"),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_run_claim_generation_input_hash"),
        sa.CheckConstraint("length(output_sha256) = 64", name="ck_run_claim_generation_output_hash"),
        sa.CheckConstraint("length(btrim(generator_model)) > 0", name="ck_run_claim_generation_model"),
        sa.CheckConstraint("jsonb_typeof(selected_evidence_ids) = 'array'", name="ck_run_claim_generation_evidence"),
        sa.CheckConstraint("jsonb_typeof(claim_ids) = 'array'", name="ck_run_claim_generation_claims"),
    )


def downgrade() -> None:
    op.drop_table("research_run_claim_generations")
