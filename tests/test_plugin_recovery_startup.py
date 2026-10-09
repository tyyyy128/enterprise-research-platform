"""Startup recovery inventory is diagnostic only, never a restart trigger."""

from __future__ import annotations

import asyncio
import logging

from deerflow_extension_api import (
    ExtensionData,
    ExtensionRuntimeDeps,
    HostPolicySnapshot,
)

import enterprise_research.plugin as plugin_module
from enterprise_research.domain import ResearchAggregate, ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)
from enterprise_research.plugin import EnterpriseResearchService


def test_postgres_startup_reports_unfinished_runs_without_scheduling_them(
    monkeypatch,
    caplog,
) -> None:
    async def run_test() -> None:
        research_repository = InMemoryResearchRepository()
        run_repository = InMemoryResearchRunRepository()
        aggregate = ResearchAggregate.create("Already running on another worker")
        await research_repository.save(aggregate)
        pending = ResearchRun.create(aggregate.task.task_id)
        active = ResearchRun.create(aggregate.task.task_id)
        active.advance(ResearchRunStatus.PLANNING)
        await run_repository.save(pending)
        await run_repository.save(active)

        class FakeEngine:
            disposed = False

            async def dispose(self) -> None:
                self.disposed = True

        engine = FakeEngine()

        async def fake_postgres_builder(self):
            self._engine = engine
            return (
                research_repository,
                run_repository,
                InMemoryResearchPlanRepository(),
            )

        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "postgres")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_SEARCH_BACKEND", "disabled")
        monkeypatch.setattr(
            EnterpriseResearchService,
            "_build_postgres_repositories",
            fake_postgres_builder,
        )
        monkeypatch.setattr(plugin_module, "build_session_factory", lambda engine: object())

        service = EnterpriseResearchService()
        deps = ExtensionRuntimeDeps(
            app_store=ExtensionData("recovery-test"),
            policy=HostPolicySnapshot(max_subagents_per_run=3),
            session_factory=object(),
        )
        with caplog.at_level(logging.WARNING, logger="enterprise_research.plugin"):
            await service.start(deps)
        try:
            executor = await service.require_run_executor()
            assert executor.active_count == 0
            assert (await run_repository.get(pending.run_id)).status == ResearchRunStatus.PENDING
            assert (await run_repository.get(active.run_id)).status == ResearchRunStatus.PLANNING
            assert "no automatic replay" in caplog.text
            assert "2 persisted unfinished research runs" in caplog.text
        finally:
            await service.stop()
        assert engine.disposed

    asyncio.run(run_test())
