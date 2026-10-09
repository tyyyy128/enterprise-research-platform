"""SQL query contract checks that need no live PostgreSQL instance."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres.models import ResearchRunRow
from enterprise_research.infrastructure.postgres.run_repository import (
    PostgresResearchRunRepository,
)


class FakeResults:
    def __init__(self, rows: list[ResearchRunRow]) -> None:
        self._rows = rows

    def all(self) -> list[ResearchRunRow]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[ResearchRunRow]) -> None:
        self.rows = rows
        self.statement = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def scalars(self, statement):
        self.statement = statement
        return FakeResults(self.rows)


def test_postgres_inventory_query_uses_status_scope_and_stable_bounded_order() -> None:
    async def run_test() -> None:
        run = ResearchRun.create(uuid4())
        run.advance(ResearchRunStatus.PLANNING)
        row = ResearchRunRow(
            id=run.run_id,
            task_id=run.task_id,
            status=run.status.value,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            error=run.error,
        )
        session = FakeSession([row])
        repository = PostgresResearchRunRepository(lambda: session)  # type: ignore[arg-type]

        found = await repository.list_unfinished(limit=7, task_id=run.task_id)

        assert found == [run]
        assert found[0] is not run
        assert session.statement is not None
        compiled = session.statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        assert "research_runs.status NOT IN" in query
        assert "research_runs.task_id =" in query
        assert "ORDER BY research_runs.updated_at ASC, research_runs.id ASC" in query
        assert "LIMIT" in query
        assert run.task_id in compiled.params.values()
        assert 7 in compiled.params.values()
        filtered_statuses = next(value for value in compiled.params.values() if isinstance(value, list))
        assert set(filtered_statuses) == {
            ResearchRunStatus.COMPLETED.value,
            ResearchRunStatus.FAILED.value,
        }

        with pytest.raises(ValueError, match="positive"):
            await repository.list_unfinished(limit=0)

    asyncio.run(run_test())
