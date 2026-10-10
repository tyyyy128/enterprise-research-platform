"""3I-1 startup refuses partially migrated databases before accepting work."""

from __future__ import annotations

import asyncio

import pytest

from enterprise_research.infrastructure.postgres.models import Base
from enterprise_research.plugin import EnterpriseResearchService


@pytest.mark.parametrize(
    "missing",
    ["research_run_claim_verifications", "research_run_report_syntheses"],
)
def test_extension_schema_preflight_rejects_missing_late_stage_tables(monkeypatch, missing):
    """No PostgreSQL connection required: inspect the SQL preflight contract."""
    seen: set[str] = set()

    class FakeConnection:
        async def scalar(self, statement, params):
            assert "to_regclass" in str(statement)
            name = params["table_name"]
            assert name.startswith("public.")
            seen.add(name.removeprefix("public."))
            return None if name == f"public.{missing}" else name

    class ConnectionManager:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_):
            return False

    class FakeEngine:
        disposed = False

        def connect(self):
            return ConnectionManager()

        async def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr("enterprise_research.plugin.PostgresSettings.from_env", lambda: object())
    monkeypatch.setattr("enterprise_research.plugin.build_async_engine", lambda _: engine)

    async def exercise():
        service = EnterpriseResearchService()
        with pytest.raises(RuntimeError, match=missing):
            await service._build_postgres_repositories()
        assert service._engine is None
        assert engine.disposed

    asyncio.run(exercise())
    assert missing in Base.metadata.tables
    assert seen <= set(Base.metadata.tables)
    assert missing in seen
