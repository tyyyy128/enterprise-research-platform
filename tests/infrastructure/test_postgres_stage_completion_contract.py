"""7A: real PostgreSQL verifies a missing pipeline stage cannot reach COMPLETED.

Opt-in only; touches a randomly generated Task/Run in a migrated LOCAL development DB.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import delete

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchPipelineStageRunner,
    ResearchPlanningService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchStageOutcome,
)
from enterprise_research.domain import ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
    or os.getenv("ENTERPRISE_RESEARCH_STAGE_CONTRACT_TESTS") != "1",
    reason="Requires two explicit flags and a migrated local PostgreSQL development database.",
)


class Planner:
    async def plan(self, *, task, run):
        return ResearchPlan(
            task_id=task.task_id,
            run_id=run.run_id,
            objective="Prove false completion is blocked",
            questions=[ResearchQuestion(question="Which stage is configured?")],
        )


def test_postgres_missing_search_produces_failed_run_and_stage_journal() -> None:
    async def exercise() -> None:
        settings = PostgresSettings.from_env()
        if settings.host not in {"127.0.0.1", "localhost", "::1"} or settings.database != "enterprise_research":
            pytest.fail("This test only allows the known local enterprise_research DEVELOPMENT database.")

        engine = build_async_engine(settings)
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            repo = PostgresResearchRepository(sessions)
            run_repo = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            app = ResearchApplicationService(repo)
            runs = ResearchRunApplicationService(repo, run_repo)
            aggregate = await app.create_task("7A: unavailable SEARCHING handler")
            research_run = await runs.create_run(aggregate.task.task_id)
            runner = ResearchPipelineStageRunner(ResearchPlanningService(repo, run_repo, plans, Planner()))
            executor = ResearchRunExecutor(runs, runner)
            try:
                executor.start(aggregate.task.task_id, research_run.run_id)

                async def poll() -> None:
                    while executor.active_count:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(poll(), timeout=10)
                persisted = await runs.get_run(aggregate.task.task_id, research_run.run_id)
                assert persisted.status == ResearchRunStatus.FAILED
                assert "SEARCHING requires" in persisted.error
                assert await plans.get_for_run(research_run.run_id) is not None
                attempts = await runs.inspect_stage_attempts(aggregate.task.task_id, research_run.run_id)
                assert [(a.stage, a.outcome) for a in attempts] == [
                    (ResearchRunStatus.PLANNING, ResearchStageOutcome.COMPLETED),
                    (ResearchRunStatus.SEARCHING, ResearchStageOutcome.FAILED),
                ]
            finally:
                await executor.shutdown()
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())
