"""Add a fenced, per-attempt stage execution journal.

Revision ID: 7c3a1e9d4b62
Revises: b65e6b3d2a90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c3a1e9d4b62"
down_revision: str | Sequence[str] | None = "b65e6b3d2a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run_stage_attempts",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "stage", "fencing_token"),
        sa.CheckConstraint(
            "stage IN ('planning', 'searching', 'extracting', 'verifying', 'writing')",
            name="ck_research_run_stage_attempts_stage",
        ),
        sa.CheckConstraint(
            "outcome IN ('started', 'completed', 'failed')",
            name="ck_research_run_stage_attempts_outcome",
        ),
        sa.CheckConstraint("fencing_token > 0", name="ck_research_run_stage_attempts_fence"),
        sa.CheckConstraint(
            "(outcome = 'started' AND finished_at IS NULL AND error IS NULL) OR "
            "(outcome = 'completed' AND finished_at IS NOT NULL AND error IS NULL) OR "
            "(outcome = 'failed' AND finished_at IS NOT NULL "
            "AND error IS NOT NULL AND length(btrim(error)) > 0)",
            name="ck_research_run_stage_attempts_finished_shape",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_research_run_stage_attempts_time_order",
        ),
    )


def downgrade() -> None:
    op.drop_table("research_run_stage_attempts")
