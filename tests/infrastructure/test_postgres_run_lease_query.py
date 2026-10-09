"""Compile the PostgreSQL fencing queries without requiring a database."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres.models import ResearchRunRow
from enterprise_research.infrastructure.postgres.run_repository import PostgresResearchRunRepository


class EmptyResult:
    def one_or_none(self):
        return None

    def scalar_one_or_none(self):
        return None


class RecordingSession:
    def __init__(self) -> None:
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def begin(self):
        return self

    async def execute(self, statement):
        self.statements.append(statement)
        return EmptyResult()


def sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_claim_renew_release_and_guarded_save_use_atomic_returning_and_db_clock() -> None:
    async def exercise() -> None:
        session = RecordingSession()
        repo = PostgresResearchRunRepository(lambda: session)  # type: ignore[arg-type]
        run = ResearchRun.create(uuid4())
        owner = uuid4()
        lease = ResearchRunLease(run.run_id, owner, 7, datetime.now(UTC) + timedelta(seconds=30))
        assert await repo.claim_lease(run.run_id, owner, ttl_seconds=42) is None
        claim = sql(session.statements[-1])
        assert "UPDATE research_runs" in claim
        assert "RETURNING research_runs.lease_fencing_token, research_runs.lease_expires_at" in claim
        assert "research_runs.lease_fencing_token +" in claim
        assert "research_runs.status =" in claim
        assert "research_runs.lease_expires_at IS NULL" in claim
        assert "clock_timestamp()" in claim
        assert "INTERVAL '1 second'" in claim
        assert 42 in session.statements[-1].compile(dialect=postgresql.dialect()).params.values()

        assert await repo.claim_lease(run.run_id, owner, ttl_seconds=15, allow_inflight=True) is None
        assert "research_runs.status =" not in sql(session.statements[-1])
        assert await repo.renew_lease(lease, ttl_seconds=20) is None
        renewal = sql(session.statements[-1])
        assert "research_runs.lease_owner_id =" in renewal
        assert "research_runs.lease_fencing_token =" in renewal
        assert "research_runs.lease_expires_at > clock_timestamp()" in renewal
        assert "RETURNING research_runs.lease_expires_at" in renewal

        assert not await repo.release_lease(lease)
        release = sql(session.statements[-1])
        assert "lease_owner_id=" in release and "lease_expires_at=" in release
        assert "research_runs.lease_fencing_token =" in release

        run.advance(ResearchRunStatus.PLANNING)
        with pytest.raises(ResearchRunLeaseLostError):
            await repo.save_guarded(run, lease, expected_status=ResearchRunStatus.PENDING)
        guarded = sql(session.statements[-1])
        assert "research_runs.task_id =" in guarded
        assert "research_runs.status =" in guarded
        assert "research_runs.lease_fencing_token =" in guarded
        assert "research_runs.lease_owner_id =" in guarded
        assert "research_runs.lease_expires_at > clock_timestamp()" in guarded
        assert "RETURNING research_runs.id" in guarded
        assert "research_runs.status NOT IN" in guarded

        for invalid in (0, -5, 3601, False, 2.5):
            with pytest.raises(ValueError, match="ttl_seconds"):
                await repo.claim_lease(run.run_id, owner, ttl_seconds=invalid)

    asyncio.run(exercise())


def test_lease_model_columns_match_migration_contract() -> None:
    table = ResearchRunRow.__table__
    assert table.columns["lease_owner_id"].nullable
    assert table.columns["lease_expires_at"].nullable
    assert not table.columns["lease_fencing_token"].nullable
    assert str(table.columns["lease_fencing_token"].server_default.arg) == "0"
