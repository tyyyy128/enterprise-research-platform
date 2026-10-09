"""Read-only recovery decisions for runs persisted by a previous executor.

The persisted status denotes *entry* into a stage, not a committed stage
result.  These decisions are inventory items, not permission to restart work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from enterprise_research.domain import ResearchRun, ResearchRunStatus


class ResearchRunRecoveryDisposition(StrEnum):
    """Required safety check before a future recovery worker can take action."""

    OWNERSHIP_CLAIM_REQUIRED = "ownership_claim_required"
    STAGE_RECONCILIATION_REQUIRED = "stage_reconciliation_required"


@dataclass(frozen=True, slots=True)
class ResearchRunRecoveryCandidate:
    """Snapshot of unfinished work; never an instruction to auto-replay it.

    ``updated_at`` is the last persisted *transition*, not a heartbeat. An
    old timestamp alone does not prove that a different worker has died.
    """

    run_id: UUID
    task_id: UUID
    status: ResearchRunStatus
    updated_at: datetime
    disposition: ResearchRunRecoveryDisposition
    stage_to_reconcile: ResearchRunStatus | None

    @classmethod
    def from_run(cls, run: ResearchRun) -> ResearchRunRecoveryCandidate:
        """Classify one nonterminal persisted run without changing its state."""
        if run.is_terminal:
            raise ValueError("Terminal research runs have no recovery candidate.")

        if run.status == ResearchRunStatus.PENDING:
            disposition = ResearchRunRecoveryDisposition.OWNERSHIP_CLAIM_REQUIRED
            stage_to_reconcile = None
        else:
            disposition = ResearchRunRecoveryDisposition.STAGE_RECONCILIATION_REQUIRED
            stage_to_reconcile = run.status

        return cls(
            run_id=run.run_id,
            task_id=run.task_id,
            status=run.status,
            updated_at=run.updated_at,
            disposition=disposition,
            stage_to_reconcile=stage_to_reconcile,
        )
