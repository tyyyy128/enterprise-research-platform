"""Research execution lifecycle models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from enterprise_research.domain.errors import (
    InvalidResearchRunTransitionError,
)
from enterprise_research.domain.models import utc_now


class ResearchRunStatus(StrEnum):
    """Lifecycle states for one execution of a research task."""

    PENDING = "pending"
    PLANNING = "planning"
    SEARCHING = "searching"
    EXTRACTING = "extracting"
    VERIFYING = "verifying"
    WRITING = "writing"
    COMPLETED = "completed"
    FAILED = "failed"


_NEXT_STATUS: dict[ResearchRunStatus, ResearchRunStatus] = {
    ResearchRunStatus.PENDING: ResearchRunStatus.PLANNING,
    ResearchRunStatus.PLANNING: ResearchRunStatus.SEARCHING,
    ResearchRunStatus.SEARCHING: ResearchRunStatus.EXTRACTING,
    ResearchRunStatus.EXTRACTING: ResearchRunStatus.VERIFYING,
    ResearchRunStatus.VERIFYING: ResearchRunStatus.WRITING,
    ResearchRunStatus.WRITING: ResearchRunStatus.COMPLETED,
}


class ResearchRun(BaseModel):
    """Tracks the lifecycle of one research execution."""

    run_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    status: ResearchRunStatus = ResearchRunStatus.PENDING

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    error: str | None = None

    @classmethod
    def create(cls, task_id: UUID) -> ResearchRun:
        """Create a pending research run for a task."""
        return cls(task_id=task_id)

    @property
    def is_terminal(self) -> bool:
        """Return whether this run can no longer advance."""
        return self.status in {
            ResearchRunStatus.COMPLETED,
            ResearchRunStatus.FAILED,
        }

    def advance(
        self,
        next_status: ResearchRunStatus,
    ) -> None:
        """Advance exactly one valid step in the research pipeline."""
        expected = _NEXT_STATUS.get(self.status)

        if expected != next_status:
            raise InvalidResearchRunTransitionError(
                f"Cannot transition research run from {self.status.value!r} to {next_status.value!r}."
            )

        now = utc_now()

        if self.started_at is None:
            self.started_at = now

        self.status = next_status
        self.updated_at = now
        self.error = None

        if next_status == ResearchRunStatus.COMPLETED:
            self.completed_at = now

    def fail(
        self,
        error: str,
    ) -> None:
        """Fail a non-terminal run with a diagnostic message."""
        message = error.strip()

        if not message:
            raise ValueError("Research run failure requires an error message.")

        if self.is_terminal:
            raise InvalidResearchRunTransitionError(
                f"Cannot fail terminal research run in state {self.status.value!r}."
            )

        now = utc_now()

        if self.started_at is None:
            self.started_at = now

        self.status = ResearchRunStatus.FAILED
        self.updated_at = now
        self.completed_at = now
        self.error = message
