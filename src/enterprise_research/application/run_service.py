"""Application service for research execution runs."""

from __future__ import annotations

from uuid import UUID

from enterprise_research.application.errors import (
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.application.run_recovery import (
    ResearchRunRecoveryCandidate,
)
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.application.stage_journal import ResearchStageAttempt, ResearchStageOutcome
from enterprise_research.domain import (
    ResearchRun,
    ResearchRunStatus,
)


class ResearchRunApplicationService:
    """Coordinates research run lifecycle use cases."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
    ) -> None:
        self._research_repository = research_repository
        self._run_repository = run_repository

    async def create_run(
        self,
        task_id: UUID,
    ) -> ResearchRun:
        """Create a pending run for an existing research task."""
        aggregate = await self._research_repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = ResearchRun.create(task_id)

        await self._run_repository.save(run)

        return run

    async def get_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> ResearchRun:
        """Return one run scoped to its owning research task."""
        run = await self._run_repository.get(run_id)

        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")

        return run

    async def advance_run(
        self,
        task_id: UUID,
        run_id: UUID,
        next_status: ResearchRunStatus,
        *,
        lease: ResearchRunLease | None = None,
    ) -> ResearchRun:
        """Advance and persist one valid run transition."""
        run = await self.get_run(
            task_id,
            run_id,
        )

        prior_status = run.status
        run.advance(next_status)

        if lease is None:
            await self._run_repository.save(run)
        else:
            await self._run_repository.save_guarded(run, lease, expected_status=prior_status)

        return run

    async def fail_run(
        self,
        task_id: UUID,
        run_id: UUID,
        error: str,
        *,
        lease: ResearchRunLease | None = None,
    ) -> ResearchRun:
        """Fail and persist a research run."""
        run = await self.get_run(
            task_id,
            run_id,
        )

        prior_status = run.status
        run.fail(error)

        if lease is None:
            await self._run_repository.save(run)
        else:
            await self._run_repository.save_guarded(run, lease, expected_status=prior_status)

        return run

    async def inspect_unfinished_runs(
        self,
        *,
        limit: int = 100,
        task_id: UUID | None = None,
    ) -> list[ResearchRunRecoveryCandidate]:
        """Inspect persisted unfinished work without claiming or re-running it.

        A PENDING run still needs an exclusive owner claim; an in-progress
        run needs stage-side-effect reconciliation. Neither is auto-resumed.
        """
        if not 1 <= limit <= 500:
            raise ValueError("Recovery inspection limit must be between 1 and 500.")

        runs = await self._run_repository.list_unfinished(limit=limit, task_id=task_id)
        return [ResearchRunRecoveryCandidate.from_run(run) for run in runs]

    async def claim_run_lease(
        self,
        task_id: UUID,
        run_id: UUID,
        owner_id: UUID,
        *,
        ttl_seconds: int,
    ) -> ResearchRunLease | None:
        """Only PENDING runs can be claimed by the normal executor."""
        await self.get_run(task_id, run_id)
        return await self._run_repository.claim_lease(run_id, owner_id, ttl_seconds=ttl_seconds)

    async def claim_reconciled_run_lease(
        self,
        task_id: UUID,
        run_id: UUID,
        owner_id: UUID,
        *,
        ttl_seconds: int,
        expected_status: ResearchRunStatus,
    ) -> ResearchRunLease | None:
        """Explicit in-flight claim, for use ONLY after safe resume inspection.

        The executor re-inspects under the new fenced lease before dispatch.
        This method is deliberately not exposed through the HTTP API.
        """
        run = await self.get_run(task_id, run_id)
        if run.is_terminal or run.status != expected_status or run.status == ResearchRunStatus.PENDING:
            return None
        return await self._run_repository.claim_lease(run_id, owner_id, ttl_seconds=ttl_seconds, allow_inflight=True)

    async def renew_run_lease(self, lease: ResearchRunLease, *, ttl_seconds: int) -> ResearchRunLease | None:
        """Heartbeat without changing the stage status."""
        return await self._run_repository.renew_lease(lease, ttl_seconds=ttl_seconds)

    async def release_run_lease(self, lease: ResearchRunLease) -> bool:
        """Best-effort owner-specific release."""
        return await self._run_repository.release_lease(lease)

    async def start_stage(
        self, task_id: UUID, run_id: UUID, stage: ResearchRunStatus, *, lease: ResearchRunLease
    ) -> ResearchStageAttempt:
        """Record entry into a stage after its fenced run transition."""
        if lease.run_id != run_id:
            raise ResearchRunLeaseLostError("Stage attempt lease refers to a different run.")
        await self.get_run(task_id, run_id)
        return await self._run_repository.start_stage(lease, stage)

    async def finish_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
        *,
        lease: ResearchRunLease,
        outcome: ResearchStageOutcome,
        error: str | None = None,
        require_artifact_proof: bool = False,
    ) -> ResearchStageAttempt:
        """Record an outcome; SUCCESS can require an atomic durable proof."""
        if lease.run_id != run_id:
            raise ResearchRunLeaseLostError("Stage attempt lease refers to a different run.")
        await self.get_run(task_id, run_id)
        if require_artifact_proof:
            return await self._run_repository.finish_stage(
                lease, stage, outcome=outcome, error=error, require_artifact_proof=True
            )
        return await self._run_repository.finish_stage(lease, stage, outcome=outcome, error=error)

    async def inspect_stage_attempts(self, task_id: UUID, run_id: UUID) -> list[ResearchStageAttempt]:
        """Read-only history. STARTED means in-doubt until separately reconciled."""
        await self.get_run(task_id, run_id)
        return await self._run_repository.list_stage_attempts(run_id)
