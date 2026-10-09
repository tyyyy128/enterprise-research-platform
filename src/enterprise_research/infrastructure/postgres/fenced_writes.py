"""Require a live, current-generation run row lock before effect writes.

The SELECT FOR UPDATE and the following mutations MUST share one transaction.
The row lock serializes with claim_lease, renew_lease and guarded Run writes.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure.postgres.models import ResearchRunRow


async def lock_fenced_stage(
    session: AsyncSession,
    lease: ResearchRunLease,
    *,
    task_id: UUID,
    stage: ResearchRunStatus,
) -> None:
    """Lock the Run and verify generation, owner, stage, expiry and task.

    PostgreSQL must re-evaluate the predicate after any concurrent row update.
    Call before *any* effect write, within the same session.begin() scope.
    """
    if not isinstance(stage, ResearchRunStatus) or stage in (
        ResearchRunStatus.PENDING,
        ResearchRunStatus.COMPLETED,
        ResearchRunStatus.FAILED,
    ):
        raise ValueError("Guarded side effects require a nonterminal research stage.")
    locked_id = await session.scalar(
        select(ResearchRunRow.id)
        .where(
            ResearchRunRow.id == lease.run_id,
            ResearchRunRow.task_id == task_id,
            ResearchRunRow.status == stage.value,
            ResearchRunRow.lease_owner_id == lease.owner_id,
            ResearchRunRow.lease_fencing_token == lease.fencing_token,
            ResearchRunRow.lease_expires_at > func.clock_timestamp(),
        )
        .with_for_update(of=ResearchRunRow)
    )
    if locked_id is None:
        raise ResearchRunLeaseLostError("Fenced side effect refused: lease lost, stale stage or wrong research task.")
