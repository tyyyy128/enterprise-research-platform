"""PostgreSQL repository for research runs, including fenced ownership."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, literal, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.application.run_lease import (
    ResearchRunLease,
    ResearchRunLeaseLostError,
    validate_lease_ttl,
)
from enterprise_research.application.stage_journal import (
    ResearchStageAttempt,
    ResearchStageJournalConflictError,
    ResearchStageOutcome,
    validate_stage,
    validate_stage_finish,
)
from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres.models import ResearchRunRow, ResearchStageAttemptRow
from enterprise_research.infrastructure.postgres.stage_completion import validate_persisted_stage_artifacts

_TERMINAL = (ResearchRunStatus.COMPLETED.value, ResearchRunStatus.FAILED.value)


def _run_from_row(row: ResearchRunRow) -> ResearchRun:
    """Hydrate a detached domain model from the durable row."""
    return ResearchRun(
        run_id=row.id,
        task_id=row.task_id,
        status=ResearchRunStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error=row.error,
    )


def _lease_expiration(ttl_seconds: int):
    """Use PostgreSQL wall time, not a potentially stale transaction snapshot."""
    return func.clock_timestamp() + literal(ttl_seconds) * literal_column("INTERVAL '1 second'")


class PostgresResearchRunRepository:
    """Persist research runs with atomic ownership and fenced transitions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, run: ResearchRun) -> None:
        """Bootstrap an unclaimed run; never bypass fencing once it was claimed.

        Legacy test/setup updates remain possible before the first claim. All
        executor transitions use save_guarded once a generation exists.
        """
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ResearchRunRow, run.run_id, with_for_update=True)

                if row is None:
                    session.add(
                        ResearchRunRow(
                            id=run.run_id,
                            task_id=run.task_id,
                            status=run.status.value,
                            created_at=run.created_at,
                            updated_at=run.updated_at,
                            started_at=run.started_at,
                            completed_at=run.completed_at,
                            error=run.error,
                            lease_fencing_token=0,
                        )
                    )
                    return

                if row.task_id != run.task_id:
                    raise ValueError("Research run cannot change its task_id.")
                if row.lease_fencing_token > 0:
                    raise ResearchRunLeaseLostError("Unfenced update refused for a claimed research run.")

                row.status = run.status.value
                row.updated_at = run.updated_at
                row.started_at = run.started_at
                row.completed_at = run.completed_at
                row.error = run.error

    async def get(self, run_id: UUID) -> ResearchRun | None:
        """Return one research run by ID."""
        async with self._session_factory() as session:
            row = await session.get(ResearchRunRow, run_id)
            return _run_from_row(row) if row is not None else None

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """List persisted nonterminal runs, oldest transition first."""
        if limit < 1:
            raise ValueError("Recovery scan limit must be positive.")

        statement = (
            select(ResearchRunRow)
            .where(ResearchRunRow.status.not_in(_TERMINAL))
            .order_by(ResearchRunRow.updated_at.asc(), ResearchRunRow.id.asc())
            .limit(limit)
        )
        if task_id is not None:
            statement = statement.where(ResearchRunRow.task_id == task_id)

        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return [_run_from_row(row) for row in rows]

    async def claim_lease(
        self, run_id: UUID, owner_id: UUID, *, ttl_seconds: int, allow_inflight: bool = False
    ) -> ResearchRunLease | None:
        """Atomically claim a pending run. In-flight takeover requires prior reconciliation.

        No caller in the runtime sets allow_inflight. It exists for an explicit,
        future recovery supervisor that has reconciled stage side effects.
        """
        validate_lease_ttl(ttl_seconds)
        statement = (
            update(ResearchRunRow)
            .where(
                ResearchRunRow.id == run_id,
                ResearchRunRow.status.not_in(_TERMINAL),
                or_(
                    ResearchRunRow.lease_expires_at.is_(None),
                    ResearchRunRow.lease_expires_at <= func.clock_timestamp(),
                ),
            )
            .values(
                lease_owner_id=owner_id,
                lease_fencing_token=ResearchRunRow.lease_fencing_token + 1,
                lease_expires_at=_lease_expiration(ttl_seconds),
            )
            .returning(ResearchRunRow.lease_fencing_token, ResearchRunRow.lease_expires_at)
        )
        if not allow_inflight:
            statement = statement.where(ResearchRunRow.status == ResearchRunStatus.PENDING.value)

        async with self._session_factory() as session:
            async with session.begin():
                result = (await session.execute(statement)).one_or_none()
                if result is None:
                    return None
                return ResearchRunLease(run_id, owner_id, result[0], result[1])

    async def renew_lease(self, lease: ResearchRunLease, *, ttl_seconds: int) -> ResearchRunLease | None:
        """Only an unexpired owner/generation can heartbeat."""
        validate_lease_ttl(ttl_seconds)
        statement = (
            update(ResearchRunRow)
            .where(
                ResearchRunRow.id == lease.run_id,
                ResearchRunRow.lease_owner_id == lease.owner_id,
                ResearchRunRow.lease_fencing_token == lease.fencing_token,
                ResearchRunRow.lease_expires_at > func.clock_timestamp(),
                ResearchRunRow.status.not_in(_TERMINAL),
            )
            .values(lease_expires_at=_lease_expiration(ttl_seconds))
            .returning(ResearchRunRow.lease_expires_at)
        )
        async with self._session_factory() as session:
            async with session.begin():
                expiry = (await session.execute(statement)).scalar_one_or_none()
                if expiry is None:
                    return None
                return ResearchRunLease(lease.run_id, lease.owner_id, lease.fencing_token, expiry)

    async def release_lease(self, lease: ResearchRunLease) -> bool:
        """Conditional release: stale generations never release the new owner."""
        statement = (
            update(ResearchRunRow)
            .where(
                ResearchRunRow.id == lease.run_id,
                ResearchRunRow.lease_owner_id == lease.owner_id,
                ResearchRunRow.lease_fencing_token == lease.fencing_token,
                ResearchRunRow.lease_expires_at > func.clock_timestamp(),
            )
            .values(lease_owner_id=None, lease_expires_at=None)
            .returning(ResearchRunRow.id)
        )
        async with self._session_factory() as session:
            async with session.begin():
                return (await session.execute(statement)).scalar_one_or_none() is not None

    async def save_guarded(
        self, run: ResearchRun, lease: ResearchRunLease, *, expected_status: ResearchRunStatus
    ) -> None:
        """Compare-and-swap lifecycle fields under a valid current generation."""
        if run.run_id != lease.run_id:
            raise ResearchRunLeaseLostError("Run ID does not match the current lease.")
        statement = (
            update(ResearchRunRow)
            .where(
                ResearchRunRow.id == run.run_id,
                ResearchRunRow.task_id == run.task_id,
                ResearchRunRow.status == expected_status.value,
                ResearchRunRow.status.not_in(_TERMINAL),
                ResearchRunRow.lease_owner_id == lease.owner_id,
                ResearchRunRow.lease_fencing_token == lease.fencing_token,
                ResearchRunRow.lease_expires_at > func.clock_timestamp(),
            )
            .values(
                status=run.status.value,
                updated_at=run.updated_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                error=run.error,
            )
            .returning(ResearchRunRow.id)
        )
        async with self._session_factory() as session:
            async with session.begin():
                changed = (await session.execute(statement)).scalar_one_or_none()
                if changed is None:
                    raise ResearchRunLeaseLostError("Run transition refused: lease lost or stale status.")

    async def _require_stage_lease(
        self, session: AsyncSession, lease: ResearchRunLease, stage: ResearchRunStatus
    ) -> None:
        """Lock the run, then validate stage/owner/token/expiry in PostgreSQL.

        The row lock serializes journal commits against claim/renew/release
        and guarded status changes. The database clock is evaluated AFTER
        a competing writer releases the row lock.
        """
        statement = (
            select(ResearchRunRow.id)
            .where(
                ResearchRunRow.id == lease.run_id,
                ResearchRunRow.status == stage.value,
                ResearchRunRow.lease_owner_id == lease.owner_id,
                ResearchRunRow.lease_fencing_token == lease.fencing_token,
                ResearchRunRow.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update(of=ResearchRunRow)
        )
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise ResearchRunLeaseLostError("Stage journal write refused: lease lost or unexpected stage.")

    @staticmethod
    def _attempt_from_row(row: ResearchStageAttemptRow) -> ResearchStageAttempt:
        return ResearchStageAttempt(
            run_id=row.run_id,
            stage=ResearchRunStatus(row.stage),
            fencing_token=row.fencing_token,
            outcome=ResearchStageOutcome(row.outcome),
            started_at=row.started_at,
            finished_at=row.finished_at,
            error=row.error,
        )

    async def start_stage(self, lease: ResearchRunLease, stage: ResearchRunStatus) -> ResearchStageAttempt:
        """Insert STARTED only after acquiring and validating the run row lock."""
        validate_stage(stage)
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_stage_lease(session, lease, stage)
                key = (lease.run_id, stage.value, lease.fencing_token)
                if await session.get(ResearchStageAttemptRow, key) is not None:
                    raise ResearchStageJournalConflictError("Stage attempt already started for this generation.")
                row = ResearchStageAttemptRow(
                    run_id=lease.run_id,
                    stage=stage.value,
                    fencing_token=lease.fencing_token,
                    outcome=ResearchStageOutcome.STARTED.value,
                    started_at=func.clock_timestamp(),
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return self._attempt_from_row(row)

    async def finish_stage(
        self,
        lease: ResearchRunLease,
        stage: ResearchRunStatus,
        *,
        outcome: ResearchStageOutcome,
        error: str | None = None,
        require_artifact_proof: bool = False,
    ) -> ResearchStageAttempt:
        """Verify durable artifacts and commit COMPLETED in one fenced transaction."""
        validate_stage(stage)
        error = validate_stage_finish(outcome, error)
        if require_artifact_proof and outcome != ResearchStageOutcome.COMPLETED:
            raise ValueError("Artifact completion proof is only available for COMPLETED outcomes.")
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_stage_lease(session, lease, stage)
                key = (lease.run_id, stage.value, lease.fencing_token)
                row = await session.get(ResearchStageAttemptRow, key, with_for_update=True)
                if row is None or row.outcome != ResearchStageOutcome.STARTED.value:
                    raise ResearchStageJournalConflictError("Stage attempt is missing or already finished.")
                if require_artifact_proof:
                    run = await session.get(ResearchRunRow, lease.run_id)
                    assert run is not None  # Protected by the Run row lock.
                    await validate_persisted_stage_artifacts(
                        session,
                        task_id=run.task_id,
                        run_id=lease.run_id,
                        stage=stage,
                        fencing_token=lease.fencing_token,
                    )
                row.outcome = outcome.value
                row.error = error
                row.finished_at = func.clock_timestamp()
                await session.flush()
                await session.refresh(row)
                return self._attempt_from_row(row)

    async def list_stage_attempts(self, run_id: UUID) -> list[ResearchStageAttempt]:
        """Read a stable ordered history without claiming or advancing a run."""
        statement = (
            select(ResearchStageAttemptRow)
            .where(ResearchStageAttemptRow.run_id == run_id)
            .order_by(
                ResearchStageAttemptRow.started_at.asc(),
                ResearchStageAttemptRow.stage.asc(),
                ResearchStageAttemptRow.fencing_token.asc(),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return [self._attempt_from_row(row) for row in rows]
