"""Run-scoped immutable report snapshots for Writing/terminal completion.

Revision ID: ea7c2f19b540
Revises: c4e7b0a6d9f2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "ea7c2f19b540"
down_revision: str | Sequence[str] | None = "c4e7b0a6d9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_report_artifacts",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("fencing_token > 0", name="ck_run_report_fencing"),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_run_report_title"),
        sa.CheckConstraint("length(btrim(body)) > 0", name="ck_run_report_body"),
        sa.CheckConstraint("length(body_sha256) = 64", name="ck_run_report_body_hash"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_run_report_payload_hash"),
        sa.CheckConstraint("jsonb_typeof(citations) = 'array'", name="ck_run_report_citations"),
    )


def downgrade() -> None:
    op.drop_table("research_run_report_artifacts")
