"""Opt-in PostgreSQL regression checks of fenced business writes."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.domain import (
    Evidence,
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.document_repository import PostgresRetrievedDocumentRepository
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1",
    reason="Requires a migrated local PostgreSQL dev database and ENTERPRISE_RESEARCH_RUN_DB_TESTS=1.",
)


def test_fenced_plan_write_rejects_stale_worker_and_preserves_old_result():
    async def run():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        runs = PostgresResearchRunRepository(sessions)
        plans = PostgresResearchPlanRepository(sessions)
        aggregate = ResearchAggregate.create("Fenced planning")
        research_run = ResearchRun.create(aggregate.task.task_id)
        try:
            await aggregates.save(aggregate)
            await runs.save(research_run)
            old = await runs.claim_lease(research_run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            research_run.advance(ResearchRunStatus.PLANNING)
            await runs.save_guarded(research_run, old, expected_status=ResearchRunStatus.PENDING)
            plan = ResearchPlan(
                task_id=aggregate.task.task_id,
                run_id=research_run.run_id,
                objective="Stable objective",
                questions=[ResearchQuestion(question="Why?")],
            )
            await plans.save_guarded(plan, old, run_repository=runs)
            assert await runs.release_lease(old)
            current = await runs.claim_lease(research_run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
            assert current is not None and current.fencing_token > old.fencing_token
            modified = plan.model_copy(update={"objective": "Stale overwrite"})
            with pytest.raises(ResearchRunLeaseLostError):
                await plans.save_guarded(modified, old, run_repository=runs)
            assert (await plans.get_for_run(research_run.run_id)).objective == "Stable objective"
            with pytest.raises(ResearchRunLeaseLostError):
                await plans.save_guarded(modified.model_copy(update={"task_id": uuid4()}), current, run_repository=runs)
            assert (await plans.get_for_run(research_run.run_id)).objective == "Stable objective"
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))
            await engine.dispose()

    asyncio.run(run())


def test_fenced_aggregate_and_document_commit_reject_old_or_cross_task_leases():
    async def run():
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregates = PostgresResearchRepository(sessions)
        runs = PostgresResearchRunRepository(sessions)
        docs = PostgresRetrievedDocumentRepository(sessions)
        aggregate = ResearchAggregate.create("Fenced aggregate and source documents")
        source = Source(source_type=SourceType.WEB, title="Reference", url="https://example.com/one")
        aggregate.add_source(source)
        research_run = ResearchRun.create(aggregate.task.task_id)
        foreign_aggregate = ResearchAggregate.create("Different task")
        foreign_source = Source(source_type=SourceType.WEB, title="Foreign", url="https://different.example.com")
        foreign_aggregate.add_source(foreign_source)
        try:
            await aggregates.save(aggregate)
            await aggregates.save(foreign_aggregate)
            await runs.save(research_run)
            old = await runs.claim_lease(research_run.run_id, uuid4(), ttl_seconds=30)
            assert old is not None
            for stage in (ResearchRunStatus.PLANNING, ResearchRunStatus.SEARCHING, ResearchRunStatus.EXTRACTING):
                before = research_run.status
                research_run.advance(stage)
                await runs.save_guarded(research_run, old, expected_status=before)
            document = RetrievedDocument.succeeded(
                source_id=source.source_id,
                requested_url="https://example.com/one",
                final_url="https://example.com/one",
                content="safe evidence",
            )
            with pytest.raises(ResearchRunLeaseLostError):
                await docs.save_guarded(
                    document, old, task_id=uuid4(), run_repository=runs, research_repository=aggregates
                )
            assert await docs.get_for_source(source.source_id) is None
            foreign_document = RetrievedDocument.succeeded(
                source_id=foreign_source.source_id,
                requested_url="https://different.example.com",
                final_url="https://different.example.com",
                content="wrong task source",
            )
            with pytest.raises(ResearchRunLeaseLostError):
                await docs.save_guarded(
                    foreign_document,
                    old,
                    task_id=aggregate.task.task_id,
                    run_repository=runs,
                    research_repository=aggregates,
                )
            assert await docs.get_for_source(foreign_source.source_id) is None
            await docs.save_guarded(
                document, old, task_id=aggregate.task.task_id, run_repository=runs, research_repository=aggregates
            )
            assert (await docs.get_for_source(source.source_id)).content == "safe evidence"
            assert await runs.release_lease(old)
            current = await runs.claim_lease(research_run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
            assert current is not None
            failed_doc = RetrievedDocument.failed(
                source_id=source.source_id, requested_url="https://example.com/one", error="stale failed"
            )
            with pytest.raises(ResearchRunLeaseLostError):
                await docs.save_guarded(
                    failed_doc,
                    old,
                    task_id=aggregate.task.task_id,
                    run_repository=runs,
                    research_repository=aggregates,
                )
            assert (await docs.get_for_source(source.source_id)).content == "safe evidence"
            before = research_run.status
            research_run.advance(ResearchRunStatus.VERIFYING)
            await runs.save_guarded(research_run, current, expected_status=before)
            modified = await aggregates.get(aggregate.task.task_id)
            modified.add_evidence(Evidence(source_id=source.source_id, content="supported", locator="chars:0-9"))
            with pytest.raises(ResearchRunLeaseLostError):
                await aggregates.save_guarded(modified, old, ResearchRunStatus.VERIFYING, run_repository=runs)
            assert not (await aggregates.get(aggregate.task.task_id)).evidence
            await aggregates.save_guarded(modified, current, ResearchRunStatus.VERIFYING, run_repository=runs)
            assert len((await aggregates.get(aggregate.task.task_id)).evidence) == 1
        finally:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        delete(ResearchTaskRow).where(
                            ResearchTaskRow.id.in_([aggregate.task.task_id, foreign_aggregate.task.task_id])
                        )
                    )
            await engine.dispose()

    asyncio.run(run())
