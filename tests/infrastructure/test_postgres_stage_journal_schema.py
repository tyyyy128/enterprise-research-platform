"""SQL compilation checks for the journal's locking and filtering protocol."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from enterprise_research.application import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure.postgres.run_repository import PostgresResearchRunRepository


class EmptyResult:
    def scalar_one_or_none(self):
        return None


class RecordingSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return EmptyResult()


def test_stage_write_locks_current_run_and_checks_lease_in_database() -> None:
    async def exercise():
        session = RecordingSession()
        repo = PostgresResearchRunRepository(lambda: session)  # type: ignore[arg-type]
        lease = ResearchRunLease(uuid4(), uuid4(), 3, datetime.now(UTC) + timedelta(seconds=30))
        with pytest.raises(ResearchRunLeaseLostError):
            await repo._require_stage_lease(session, lease, ResearchRunStatus.PLANNING)
        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF research_runs" in sql
        assert "research_runs.id =" in sql
        assert "research_runs.status =" in sql
        assert "research_runs.lease_owner_id =" in sql
        assert "research_runs.lease_fencing_token =" in sql
        assert "research_runs.lease_expires_at > clock_timestamp()" in sql
        params = session.statements[0].compile(dialect=postgresql.dialect()).params
        assert lease.run_id in params.values()
        assert lease.owner_id in params.values()
        assert lease.fencing_token in params.values()
        assert ResearchRunStatus.PLANNING.value in params.values()

    asyncio.run(exercise())
