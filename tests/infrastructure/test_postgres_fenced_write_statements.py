"""Compile actual fencing query to PostgreSQL SQL without a database."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage


class CapturingSession:
    def __init__(self, result):
        self.result = result
        self.statement = None

    async def scalar(self, statement):
        self.statement = statement
        return self.result


def test_postgres_effect_guard_checks_owner_stage_and_expiry_under_row_lock():
    async def check():
        run_id = uuid4()
        task_id = uuid4()
        owner_id = uuid4()
        lease = ResearchRunLease(run_id, owner_id, 7, datetime.now(UTC) + timedelta(minutes=1))
        capture = CapturingSession(run_id)
        await lock_fenced_stage(capture, lease, task_id=task_id, stage=ResearchRunStatus.SEARCHING)
        sql = str(capture.statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF research_runs" in sql
        assert "clock_timestamp()" in sql
        assert "lease_fencing_token" in sql
        assert "lease_owner_id" in sql
        assert "lease_expires_at" in sql
        assert "task_id" in sql
        assert "status" in sql

        capture = CapturingSession(None)
        with pytest.raises(ResearchRunLeaseLostError):
            await lock_fenced_stage(capture, lease, task_id=task_id, stage=ResearchRunStatus.SEARCHING)
        assert capture.statement is not None

        capture = CapturingSession(run_id)
        with pytest.raises(ValueError, match="nonterminal"):
            await lock_fenced_stage(capture, lease, task_id=task_id, stage=ResearchRunStatus.COMPLETED)
        assert capture.statement is None

    asyncio.run(check())
