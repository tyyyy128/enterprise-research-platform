"""Add fenced leases to persisted research runs.

Revision ID: b65e6b3d2a90
Revises: 224c6bf5fd29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b65e6b3d2a90"
down_revision: str | Sequence[str] | None = "224c6bf5fd29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("research_runs", sa.Column("lease_owner_id", sa.UUID(), nullable=True))
    op.add_column("research_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "research_runs",
        sa.Column("lease_fencing_token", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_research_runs_lease_pair",
        "research_runs",
        "(lease_owner_id IS NULL) = (lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_research_runs_lease_fence_nonnegative",
        "research_runs",
        "lease_fencing_token >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_research_runs_lease_fence_nonnegative", "research_runs", type_="check")
    op.drop_constraint("ck_research_runs_lease_pair", "research_runs", type_="check")
    op.drop_column("research_runs", "lease_fencing_token")
    op.drop_column("research_runs", "lease_expires_at")
    op.drop_column("research_runs", "lease_owner_id")
