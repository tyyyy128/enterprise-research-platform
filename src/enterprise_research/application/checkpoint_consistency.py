"""Read-only reconciliation of Stage Journal and optional Graph checkpoints.

A checkpoint is not a business commit receipt.  A completed stage journal is
also insufficient without the stage-specific durable artifact check performed
by ResearchSafeResumeInspector.  This module never reads Graph state payloads,
acquires leases, writes checkpoints or retries external calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from enterprise_research.application.graph_checkpoint import (
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointConflictError,
    ResearchGraphCheckpointReader,
    ResearchGraphCheckpointReference,
)
from enterprise_research.application.stage_journal import ResearchStageAttempt, ResearchStageOutcome, validate_stage
from enterprise_research.domain import ResearchRunStatus

logger = logging.getLogger(__name__)


class ResearchCheckpointObservation(StrEnum):
    """Graph observation status, separate from the authoritative journal."""

    NOT_CHECKED = "journal_ambiguous_no_checkpoint_lookup"
    JOURNAL_SCOPE_CONFLICT = "journal_attempt_wrong_run"
    NOT_CONFIGURED = "checkpoint_reader_not_configured"
    ABSENT = "checkpoint_absent"
    PRESENT = "checkpoint_observed"
    IDENTITY_CONFLICT = "checkpoint_identity_conflict"
    UNAVAILABLE = "checkpoint_reader_unavailable"


@dataclass(frozen=True, slots=True)
class ResearchStageCheckpointReconciliation:
    """Audit-safe stage/Graph observation; no Graph payload or business contents."""

    stage: ResearchRunStatus
    attempt_count: int
    journal_outcome: ResearchStageOutcome | None
    fencing_token: int | None
    checkpoint: ResearchCheckpointObservation
    checkpoint_id: str | None = None

    @property
    def may_verify_artifacts(self) -> bool:
        """Only allows an artifact *check*, never directly authorizes resume."""
        return (
            self.attempt_count == 1
            and self.journal_outcome == ResearchStageOutcome.COMPLETED
            and self.checkpoint
            in {
                ResearchCheckpointObservation.NOT_CONFIGURED,
                ResearchCheckpointObservation.ABSENT,
                ResearchCheckpointObservation.PRESENT,
            }
        )

    @property
    def pause_reason(self) -> str | None:
        if self.checkpoint == ResearchCheckpointObservation.JOURNAL_SCOPE_CONFLICT:
            return f"Stage journal belongs to a different Run for {self.stage.value}."
        if self.attempt_count != 1 or self.journal_outcome != ResearchStageOutcome.COMPLETED:
            return f"Stage {self.stage.value} has missing, duplicate, failed or in-doubt attempts."
        if self.checkpoint == ResearchCheckpointObservation.IDENTITY_CONFLICT:
            return f"Graph checkpoint identity conflicts with stage journal for {self.stage.value}."
        if self.checkpoint == ResearchCheckpointObservation.UNAVAILABLE:
            return f"Graph checkpoint observation unavailable for {self.stage.value}; resume stays paused."
        return None

    @property
    def proof_marker(self) -> tuple[str, str, str, int | None, str, str | None]:
        """Includes missing/present state so pre/post lease-check changes are visible."""
        return (
            "checkpoint_reconciliation",
            self.stage.value,
            self.journal_outcome.value if self.journal_outcome is not None else "missing_or_duplicate",
            self.fencing_token,
            self.checkpoint.value,
            self.checkpoint_id,
        )


async def reconcile_stage_checkpoint(
    *,
    task_id: UUID,
    run_id: UUID,
    stage: ResearchRunStatus,
    attempts: list[ResearchStageAttempt],
    reader: ResearchGraphCheckpointReader | None,
) -> ResearchStageCheckpointReconciliation:
    """Classify the two independent persistence observations, without writes.

    Journal ambiguity has no uniquely trustworthy fencing generation, so do
    not construct a speculative Graph namespace or query a different Run.
    A checkpoint can be present even while its journal is STARTED or FAILED;
    that remains in-doubt regardless of Graph contents.
    """
    validate_stage(stage)
    entries = [entry for entry in attempts if entry.stage == stage]
    if any(entry.run_id != run_id for entry in attempts):
        return ResearchStageCheckpointReconciliation(
            stage, len(entries), None, None, ResearchCheckpointObservation.JOURNAL_SCOPE_CONFLICT
        )
    if len(entries) != 1:
        return ResearchStageCheckpointReconciliation(
            stage, len(entries), None, None, ResearchCheckpointObservation.NOT_CHECKED
        )

    attempt = entries[0]
    if reader is None:
        return ResearchStageCheckpointReconciliation(
            stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.NOT_CONFIGURED
        )

    binding = ResearchGraphCheckpointBinding(task_id, run_id, stage, attempt.fencing_token)
    try:
        reference = await reader.latest(binding)
    except ResearchGraphCheckpointConflictError:
        return ResearchStageCheckpointReconciliation(
            stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.IDENTITY_CONFLICT
        )
    except Exception as exc:
        # Do not expose saver errors or payloads through the recovery API.
        # Do not catch cancellation (BaseException), which must propagate.
        logger.warning(
            "Checkpoint observation unavailable for run %s stage %s (%s)",
            run_id,
            stage.value,
            type(exc).__name__,
        )
        return ResearchStageCheckpointReconciliation(
            stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.UNAVAILABLE
        )

    if reference is None:
        return ResearchStageCheckpointReconciliation(
            stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.ABSENT
        )
    if (
        not isinstance(reference, ResearchGraphCheckpointReference)
        or reference.binding != binding
        or not isinstance(reference.checkpoint_id, str)
        or not reference.checkpoint_id.strip()
    ):
        return ResearchStageCheckpointReconciliation(
            stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.IDENTITY_CONFLICT
        )
    return ResearchStageCheckpointReconciliation(
        stage, 1, attempt.outcome, attempt.fencing_token, ResearchCheckpointObservation.PRESENT, reference.checkpoint_id
    )
