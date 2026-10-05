from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.domain import (
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.plan_repository import (
    PostgresResearchPlanRepository,
)

RUN_DB_TESTS = os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason=("Set ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 to run PostgreSQL integration tests."),
)


def test_postgres_plan_repository_round_trip() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)

        research_repository = PostgresResearchRepository(session_factory)
        run_repository = PostgresResearchRunRepository(session_factory)
        plan_repository = PostgresResearchPlanRepository(session_factory)

        aggregate = ResearchAggregate.create("Research durable execution")

        research_run = ResearchRun.create(aggregate.task.task_id)

        research_run.advance(ResearchRunStatus.PLANNING)

        plan = ResearchPlan(
            task_id=aggregate.task.task_id,
            run_id=research_run.run_id,
            objective=("Assess durable execution."),
            questions=[
                ResearchQuestion(question=("How does recovery work?")),
                ResearchQuestion(question=("What are the trade-offs?")),
            ],
        )

        try:
            await research_repository.save(aggregate)
            await run_repository.save(research_run)
            await plan_repository.save(plan)

            loaded = await plan_repository.get_for_run(research_run.run_id)

            assert loaded == plan

            assert [item.question for item in loaded.questions] == [
                "How does recovery work?",
                "What are the trade-offs?",
            ]

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())


def test_postgres_plan_rejects_task_run_mismatch() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)

        research_repository = PostgresResearchRepository(session_factory)
        run_repository = PostgresResearchRunRepository(session_factory)
        plan_repository = PostgresResearchPlanRepository(session_factory)

        aggregate = ResearchAggregate.create("Research durable execution")

        research_run = ResearchRun.create(aggregate.task.task_id)

        bad_plan = ResearchPlan(
            task_id=uuid4(),
            run_id=research_run.run_id,
            objective="Invalid ownership.",
            questions=[ResearchQuestion(question="Should this persist?")],
        )

        try:
            await research_repository.save(aggregate)
            await run_repository.save(research_run)

            with pytest.raises(
                ValueError,
                match="task_id",
            ):
                await plan_repository.save(bad_plan)

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())
