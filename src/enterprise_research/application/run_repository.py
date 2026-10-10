"""Repository contract for research execution runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_journal import ResearchStageAttempt, ResearchStageOutcome
from enterprise_research.domain import ResearchRun, ResearchRunStatus


class ResearchRunRepository(Protocol):
    """Persistence contract for research execution lifecycle records."""

    async def save(self, run: ResearchRun) -> None:
        """Persist a research run."""

    async def get(self, run_id: UUID) -> ResearchRun | None:
        """Return a research run by ID."""

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """Oldest nonterminal runs first; read-only and bounded."""

    async def claim_lease(
        self, run_id: UUID, owner_id: UUID, *, ttl_seconds: int, allow_inflight: bool = False
    ) -> ResearchRunLease | None:
        """Atomically acquire a new generation, normally only for PENDING runs."""

    async def renew_lease(self, lease: ResearchRunLease, *, ttl_seconds: int) -> ResearchRunLease | None:
        """Extend the current, unexpired lease; return None on loss."""

    async def release_lease(self, lease: ResearchRunLease) -> bool:
        """Best-effort release, never releasing a different generation."""

    async def save_guarded(
        self, run: ResearchRun, lease: ResearchRunLease, *, expected_status: ResearchRunStatus
    ) -> None:
        """CAS a run transition only if the current lease is valid."""

    async def start_stage(self, lease: ResearchRunLease, stage: ResearchRunStatus) -> ResearchStageAttempt:
        """Atomically append one STARTED attempt if current lease and stage match."""

    async def finish_stage(
        self,
        lease: ResearchRunLease,
        stage: ResearchRunStatus,
        *,
        outcome: ResearchStageOutcome,
        error: str | None = None,
        require_artifact_proof: bool = False,
    ) -> ResearchStageAttempt:
        """Finish under the live lease; optionally require atomic artifact proof."""

    async def list_stage_attempts(self, run_id: UUID) -> list[ResearchStageAttempt]:
        """Return persisted stage attempts for recovery inspection, never replay."""
