"""3H write-once report synthesis plans tied to fenced Run reports.

Revision ID: c8d4a762e901
Revises: f3a7c9d25168
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c8d4a762e901"
down_revision: str | Sequence[str] | None = "f3a7c9d25168"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_report_syntheses",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("plan", JSONB(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_run_report_artifacts.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("fencing_token > 0", name="ck_report_synthesis_fencing"),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_report_synthesis_input"),
        sa.CheckConstraint("length(receipt_sha256) = 64", name="ck_report_synthesis_receipt"),
        sa.CheckConstraint("length(btrim(model)) > 0", name="ck_report_synthesis_model"),
        sa.CheckConstraint("jsonb_typeof(plan) = 'object'", name="ck_report_synthesis_plan"),
    )


def downgrade() -> None:
    op.drop_table("research_run_report_syntheses")
