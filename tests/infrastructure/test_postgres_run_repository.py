from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.domain import (
    ResearchAggregate,
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

RUN_DB_TESTS = os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason=("Set ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 to run PostgreSQL integration tests."),
)


def test_postgres_run_repository_round_trip() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)

        aggregate_repository = PostgresResearchRepository(session_factory)
        run_repository = PostgresResearchRunRepository(session_factory)

        aggregate = ResearchAggregate.create("Research durable execution")
        research_run = ResearchRun.create(aggregate.task.task_id)

        try:
            await aggregate_repository.save(aggregate)

            assert await run_repository.get(uuid4()) is None

            await run_repository.save(research_run)

            loaded = await run_repository.get(research_run.run_id)

            assert loaded is not None
            assert loaded == research_run

            research_run.advance(ResearchRunStatus.PLANNING)

            await run_repository.save(research_run)

            updated = await run_repository.get(research_run.run_id)

            assert updated is not None
            assert updated.status == ResearchRunStatus.PLANNING
            assert updated.started_at is not None
            assert updated.error is None

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())


def test_aggregate_resave_preserves_research_run() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)

        aggregate_repository = PostgresResearchRepository(session_factory)
        run_repository = PostgresResearchRunRepository(session_factory)

        aggregate = ResearchAggregate.create("Original research query")
        research_run = ResearchRun.create(aggregate.task.task_id)

        try:
            await aggregate_repository.save(aggregate)
            await run_repository.save(research_run)

            aggregate.task.query = "Updated research query"

            await aggregate_repository.save(aggregate)
            await aggregate_repository.save(aggregate)

            preserved = await run_repository.get(research_run.run_id)

            assert preserved is not None
            assert preserved.run_id == research_run.run_id
            assert preserved.task_id == aggregate.task.task_id
            assert preserved.status == ResearchRunStatus.PENDING

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))

            await engine.dispose()

    asyncio.run(run())


def test_postgres_recovery_inventory_survives_repository_recreation() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)
        aggregate_repository = PostgresResearchRepository(session_factory)
        repository = PostgresResearchRunRepository(session_factory)
        aggregate = ResearchAggregate.create("Recover unfinished research runs")
        pending = ResearchRun.create(aggregate.task.task_id)
        in_flight = ResearchRun.create(aggregate.task.task_id)
        failed = ResearchRun.create(aggregate.task.task_id)

        try:
            await aggregate_repository.save(aggregate)
            in_flight.advance(ResearchRunStatus.PLANNING)
            failed.fail("already terminated")
            for research_run in (pending, in_flight, failed):
                await repository.save(research_run)

            # Simulates a fresh adapter after a process restart. This is a
            # read-only scan and must not claim or advance either run.
            reopened = PostgresResearchRunRepository(session_factory)
            candidates = await reopened.list_unfinished(limit=10, task_id=aggregate.task.task_id)
            assert {run.run_id for run in candidates} == {pending.run_id, in_flight.run_id}
            assert (await reopened.get(in_flight.run_id)).status == ResearchRunStatus.PLANNING
            assert (await reopened.get(failed.run_id)).status == ResearchRunStatus.FAILED

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(run())
