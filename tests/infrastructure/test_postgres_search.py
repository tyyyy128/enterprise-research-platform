from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

import pytest
from sqlalchemy import delete

from enterprise_research.application import (
    ResearchSearchService,
    SearchResult,
)
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


class StaticSearchProvider:
    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> Sequence[SearchResult]:
        assert query == "How does recovery work?"
        assert max_results == 3

        return [
            SearchResult(
                title="Recovery documentation",
                url="https://example.com/recovery",
                snippet=("Checkpoint and recovery semantics."),
                score=0.92,
                provider="fake",
            )
        ]


def test_postgres_search_persists_sources_and_preserves_run_plan() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()

        engine = build_async_engine(settings)

        session_factory = build_session_factory(engine)

        research_repository = PostgresResearchRepository(session_factory)

        run_repository = PostgresResearchRunRepository(session_factory)

        plan_repository = PostgresResearchPlanRepository(session_factory)

        aggregate = ResearchAggregate.create("Evaluate durable execution")

        research_run = ResearchRun.create(aggregate.task.task_id)

        plan = ResearchPlan(
            task_id=aggregate.task.task_id,
            run_id=research_run.run_id,
            objective=("Evaluate durable execution"),
            questions=[
                ResearchQuestion(
                    question=("How does recovery work?"),
                    rationale=("Assess recovery semantics."),
                )
            ],
        )

        try:
            await research_repository.save(aggregate)

            research_run.advance(ResearchRunStatus.PLANNING)

            await run_repository.save(research_run)

            await plan_repository.save(plan)

            research_run.advance(ResearchRunStatus.SEARCHING)

            await run_repository.save(research_run)

            service = ResearchSearchService(
                research_repository,
                run_repository,
                plan_repository,
                StaticSearchProvider(),
                max_results_per_question=3,
            )

            sources = await service.search_run(
                aggregate.task.task_id,
                research_run.run_id,
            )

            assert len(sources) == 1

            loaded_aggregate = await research_repository.get(aggregate.task.task_id)

            assert loaded_aggregate is not None
            assert len(loaded_aggregate.sources) == 1

            preserved_run = await run_repository.get(research_run.run_id)

            assert preserved_run is not None

            assert preserved_run.status == ResearchRunStatus.SEARCHING

            preserved_plan = await plan_repository.get_for_run(research_run.run_id)

            assert preserved_plan == plan

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())
