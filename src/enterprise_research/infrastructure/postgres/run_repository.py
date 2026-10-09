"""PostgreSQL repository for research runs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.domain import (
    ResearchRun,
    ResearchRunStatus,
)
from enterprise_research.infrastructure.postgres.models import (
    ResearchRunRow,
)


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


class PostgresResearchRunRepository:
    """Persist research execution lifecycle records in PostgreSQL."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def save(self, run: ResearchRun) -> None:
        """Insert or update one research run atomically."""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    ResearchRunRow,
                    run.run_id,
                )

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
                        )
                    )
                    return

                if row.task_id != run.task_id:
                    raise ValueError("Research run cannot change its task_id.")

                row.status = run.status.value
                row.updated_at = run.updated_at
                row.started_at = run.started_at
                row.completed_at = run.completed_at
                row.error = run.error

    async def get(
        self,
        run_id: UUID,
    ) -> ResearchRun | None:
        """Return one research run by ID."""
        async with self._session_factory() as session:
            row = await session.get(
                ResearchRunRow,
                run_id,
            )

            if row is None:
                return None

            return _run_from_row(row)

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """List persisted nonterminal runs, oldest transition first."""
        if limit < 1:
            raise ValueError("Recovery scan limit must be positive.")

        statement = (
            select(ResearchRunRow)
            .where(ResearchRunRow.status.not_in((ResearchRunStatus.COMPLETED.value, ResearchRunStatus.FAILED.value)))
            .order_by(ResearchRunRow.updated_at.asc(), ResearchRunRow.id.asc())
            .limit(limit)
        )

        if task_id is not None:
            statement = statement.where(ResearchRunRow.task_id == task_id)

        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return [_run_from_row(row) for row in rows]
