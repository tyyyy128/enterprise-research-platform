"""Durable audit records for each generation's research-stage attempt.

A completed journal entry means the stage runner returned and the *journal*
commit succeeded. It does not certify that external calls or aggregate writes
were committed atomically; those require the later fenced-side-effects phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from enterprise_research.domain import ResearchRunStatus

STAGE_STATUSES = frozenset(
    {
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
    }
)


class ResearchStageOutcome(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchStageJournalConflictError(RuntimeError):
    """An attempt already started, finished or is not in the expected state."""


def validate_stage(stage: ResearchRunStatus) -> None:
    if not isinstance(stage, ResearchRunStatus) or stage not in STAGE_STATUSES:
        raise ValueError("A stage journal entry requires a nonterminal research stage.")


def validate_stage_finish(outcome: ResearchStageOutcome, error: str | None) -> str | None:
    if not isinstance(outcome, ResearchStageOutcome):
        raise ValueError("Stage outcome must be an explicit ResearchStageOutcome.")
    if outcome == ResearchStageOutcome.COMPLETED:
        if error is not None:
            raise ValueError("A completed stage cannot have an error.")
        return None
    if outcome == ResearchStageOutcome.FAILED:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("A failed stage requires a nonblank error.")
        return error.strip()
    raise ValueError("A stage can finish only as completed or failed.")


@dataclass(frozen=True, slots=True)
class ResearchStageAttempt:
    run_id: UUID
    stage: ResearchRunStatus
    fencing_token: int
    outcome: ResearchStageOutcome
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        validate_stage(self.stage)
        if not isinstance(self.outcome, ResearchStageOutcome):
            raise ValueError("Stage attempt outcome must be a ResearchStageOutcome.")
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise ValueError("Stage attempts require a positive fencing token.")
        for instant in (self.started_at, self.finished_at):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("Stage attempt timestamps must be timezone-aware.")
        if self.outcome == ResearchStageOutcome.STARTED:
            if self.finished_at is not None or self.error is not None:
                raise ValueError("Started stage attempts cannot have a finish or an error.")
        elif self.finished_at is None:
            raise ValueError("Finished stage attempts require a finish timestamp.")
        else:
            validate_stage_finish(self.outcome, self.error)
            if self.finished_at < self.started_at:
                raise ValueError("Stage attempt finish cannot precede its start.")
