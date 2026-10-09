"""Real in-memory repository and application side-effect fences."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application.planning_service import ResearchPlanningService
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.application.run_service import ResearchRunApplicationService
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
from enterprise_research.infrastructure.memory_document_repository import InMemoryRetrievedDocumentRepository
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository
from enterprise_research.infrastructure.memory_repository import InMemoryResearchRepository
from enterprise_research.infrastructure.memory_run_repository import InMemoryResearchRunRepository


async def setup(stage=ResearchRunStatus.PLANNING):
    aggregate_repo = InMemoryResearchRepository()
    run_repo = InMemoryResearchRunRepository()
    plan_repo = InMemoryResearchPlanRepository()
    documents = InMemoryRetrievedDocumentRepository()
    aggregate = ResearchAggregate.create("Fenced persistence")
    source = Source(source_type=SourceType.WEB, title="Reference", url="https://example.com")
    aggregate.add_source(source)
    await aggregate_repo.save(aggregate)
    run = ResearchRun.create(aggregate.task.task_id)
    await run_repo.save(run)
    app = ResearchRunApplicationService(aggregate_repo, run_repo)
    lease = await app.claim_run_lease(aggregate.task.task_id, run.run_id, uuid4(), ttl_seconds=60)
    assert lease is not None
    for status in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
    ):
        await app.advance_run(aggregate.task.task_id, run.run_id, status, lease=lease)
        if status == stage:
            break
    return aggregate_repo, run_repo, plan_repo, documents, aggregate, source, run, lease


def test_valid_lease_persists_plan_and_stale_generation_cannot_replace_it():
    async def run():
        agg, repo, plans, _docs, task, _source, research_run, lease = await setup()

        class Planner:
            async def plan(self, *, task, run):
                return ResearchPlan(
                    task_id=task.task_id,
                    run_id=run.run_id,
                    objective="Protected plan",
                    questions=[ResearchQuestion(question="How?")],
                )

        service = ResearchPlanningService(agg, repo, plans, Planner())
        plan = await service.create_plan(task.task.task_id, research_run.run_id, lease=lease)
        assert (await plans.get_for_run(research_run.run_id)).objective == "Protected plan"
        await repo.release_lease(lease)
        next_lease = await repo.claim_lease(research_run.run_id, uuid4(), ttl_seconds=60, allow_inflight=True)
        assert next_lease is not None and next_lease.fencing_token > lease.fencing_token
        plan2 = plan.model_copy(update={"objective": "Stale overwrite"})
        with pytest.raises(ResearchRunLeaseLostError):
            await plans.save_guarded(plan2, lease, run_repository=repo)
        assert (await plans.get_for_run(research_run.run_id)).objective == "Protected plan"

    asyncio.run(run())


def test_document_effect_requires_right_stage_and_current_owner():
    async def run():
        _agg, repo, _plans, docs, task, source, research_run, lease = await setup(ResearchRunStatus.EXTRACTING)
        doc = RetrievedDocument.succeeded(
            source_id=source.source_id,
            requested_url="https://example.com",
            final_url="https://example.com",
            content="source text",
        )
        with pytest.raises(ResearchRunLeaseLostError):
            await docs.save_guarded(doc, lease, task_id=uuid4(), run_repository=repo, research_repository=_agg)
        await docs.save_guarded(doc, lease, task_id=task.task.task_id, run_repository=repo, research_repository=_agg)
        assert (await docs.get_for_source(source.source_id)).content == "source text"
        await repo.release_lease(lease)
        replacement = await repo.claim_lease(research_run.run_id, uuid4(), ttl_seconds=60, allow_inflight=True)
        assert replacement is not None
        failed_doc = RetrievedDocument.failed(
            source_id=source.source_id, requested_url="https://example.com", error="stale"
        )
        with pytest.raises(ResearchRunLeaseLostError):
            await docs.save_guarded(
                failed_doc, lease, task_id=task.task.task_id, run_repository=repo, research_repository=_agg
            )
        assert (await docs.get_for_source(source.source_id)).content == "source text"

    asyncio.run(run())


def test_aggregate_effect_is_fenced_at_verifying_stage():
    async def run():
        aggregates, repo, _plans, _docs, task, source, research_run, lease = await setup(ResearchRunStatus.VERIFYING)
        new_version = await aggregates.get(task.task.task_id)
        new_version.add_evidence(Evidence(source_id=source.source_id, content="supported", locator="chars:0-9"))
        with pytest.raises(ResearchRunLeaseLostError):
            await aggregates.save_guarded(new_version, lease, ResearchRunStatus.SEARCHING, run_repository=repo)
        assert not (await aggregates.get(task.task.task_id)).evidence
        await aggregates.save_guarded(new_version, lease, ResearchRunStatus.VERIFYING, run_repository=repo)
        assert len((await aggregates.get(task.task.task_id)).evidence) == 1
        await repo.release_lease(lease)
        next_owner = await repo.claim_lease(research_run.run_id, uuid4(), ttl_seconds=60, allow_inflight=True)
        assert next_owner is not None
        another = await aggregates.get(task.task.task_id)
        another.evidence.clear()
        with pytest.raises(ResearchRunLeaseLostError):
            await aggregates.save_guarded(another, lease, ResearchRunStatus.VERIFYING, run_repository=repo)
        assert len((await aggregates.get(task.task.task_id)).evidence) == 1

    asyncio.run(run())


def test_fenced_memory_side_effect_blocks_lease_takeover_until_write_finishes():
    async def run():
        aggregates, repo, _plans, _docs, task, source, research_run, lease = await setup(ResearchRunStatus.SEARCHING)
        entered, finish = asyncio.Event(), asyncio.Event()
        original_save = aggregates.save

        async def blocking_save(aggregate):
            entered.set()
            await finish.wait()
            await original_save(aggregate)

        aggregates.save = blocking_save
        attempt = asyncio.create_task(
            aggregates.save_guarded(task, lease, ResearchRunStatus.SEARCHING, run_repository=repo)
        )
        await asyncio.wait_for(entered.wait(), 1)
        release = asyncio.create_task(repo.release_lease(lease))
        await asyncio.sleep(0)
        assert not release.done()
        finish.set()
        await asyncio.wait_for(attempt, 1)
        assert await asyncio.wait_for(release, 1)

    asyncio.run(run())


def test_search_service_passes_live_lease_to_aggregate_save():
    from enterprise_research.application.search import SearchResult
    from enterprise_research.application.search_service import ResearchSearchService

    async def run():
        aggregates, repo, plans, _docs, task, _source, research_run, lease = await setup(ResearchRunStatus.SEARCHING)
        await plans.save(
            ResearchPlan(
                task_id=task.task.task_id,
                run_id=research_run.run_id,
                objective="Search",
                questions=[ResearchQuestion(question="What's new?")],
            )
        )

        class Provider:
            async def search(self, query, *, max_results):
                assert query == "What's new?"
                return [SearchResult(title="New", url="https://example.org/new", provider="controlled")]

        service = ResearchSearchService(aggregates, repo, plans, Provider())
        await service.search_run(task.task.task_id, research_run.run_id, lease=lease)
        persisted = await aggregates.get(task.task.task_id)
        assert len(persisted.sources) == 2
        assert any(str(source.url) == "https://example.org/new" for source in persisted.sources.values())

    asyncio.run(run())


def test_retrieval_service_passes_live_lease_to_document_save():
    from enterprise_research.application.retrieval import WebFetchResult
    from enterprise_research.application.retrieval_service import ResearchRetrievalService

    async def run():
        aggregates, repo, _plans, documents, task, source, research_run, lease = await setup(
            ResearchRunStatus.EXTRACTING
        )

        class Fetcher:
            async def fetch(self, url):
                return WebFetchResult(
                    requested_url=url, final_url=url, content="Verified text", content_type="text/markdown"
                )

        service = ResearchRetrievalService(aggregates, repo, documents, Fetcher())
        result = await service.retrieve_run(task.task.task_id, research_run.run_id, lease=lease)
        assert result.succeeded_count == 1
        assert (await documents.get_for_source(source.source_id)).content == "Verified text"

    asyncio.run(run())


def test_expired_lease_cannot_commit_a_plan_even_without_a_new_owner():
    from datetime import UTC, datetime, timedelta

    async def run():
        class Clock:
            now = datetime.now(UTC)

            def __call__(self):
                return self.now

        clock = Clock()
        repo = InMemoryResearchRunRepository(clock=clock)
        plans = InMemoryResearchPlanRepository()
        task = ResearchAggregate.create("Lease expiry")
        research_run = ResearchRun.create(task.task.task_id)
        await repo.save(research_run)
        lease = await repo.claim_lease(research_run.run_id, uuid4(), ttl_seconds=1)
        assert lease is not None
        research_run.advance(ResearchRunStatus.PLANNING)
        await repo.save_guarded(research_run, lease, expected_status=ResearchRunStatus.PENDING)
        clock.now += timedelta(seconds=2)
        plan = ResearchPlan(
            task_id=task.task.task_id,
            run_id=research_run.run_id,
            objective="Uncommitted",
            questions=[ResearchQuestion(question="Why?")],
        )
        with pytest.raises(ResearchRunLeaseLostError):
            await plans.save_guarded(plan, lease, run_repository=repo)
        assert await plans.get_for_run(research_run.run_id) is None

    asyncio.run(run())


def test_memory_document_source_must_belong_to_current_task():
    async def run():
        aggregate_repo, repo, _plans, docs, task, _source, _run, lease = await setup(ResearchRunStatus.EXTRACTING)
        external = Source(source_type=SourceType.WEB, title="Foreign", url="https://foreign.example.com")
        document = RetrievedDocument.succeeded(
            source_id=external.source_id,
            requested_url="https://foreign.example.com",
            final_url="https://foreign.example.com",
            content="not this task",
        )
        with pytest.raises(ResearchRunLeaseLostError):
            await docs.save_guarded(
                document, lease, task_id=task.task.task_id, run_repository=repo, research_repository=aggregate_repo
            )
        assert await docs.get_for_source(external.source_id) is None

    asyncio.run(run())
