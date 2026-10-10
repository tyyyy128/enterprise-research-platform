"""Conservative, opt-in recovery across durable stage and artifact boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from enterprise_research.application import ResearchRunApplicationService, ResearchRunExecutor, ResearchStageOutcome
from enterprise_research.application.errors import ResearchRunNotFoundError
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import (
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRunStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


class ManualClock:
    def __init__(self):
        self.now = datetime(2026, 10, 9, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class RecordingRunner:
    def __init__(self, *, block_stage=None):
        self.calls = []
        self.block_stage = block_stage
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run_stage(self, task_id, run_id, stage, *, lease=None):
        assert lease is not None
        self.calls.append(stage)
        if stage == self.block_stage:
            self.entered.set()
            await self.release.wait()


async def fixture(*, stage=ResearchRunStatus.PLANNING, with_artifacts=True, clock=None):
    aggregate = ResearchAggregate.create("Safe resume artifact reconciliation")
    research = InMemoryResearchRepository()
    repo = InMemoryResearchRunRepository(clock=clock or ManualClock())
    plans = InMemoryResearchPlanRepository()
    documents = InMemoryRetrievedDocumentRepository()
    await research.save(aggregate)
    runs = ResearchRunApplicationService(research, repo)
    run = await runs.create_run(aggregate.task.task_id)
    question = ResearchQuestion(question="What changed?", rationale="Check provenance")
    plan = ResearchPlan(
        task_id=aggregate.task.task_id,
        run_id=run.run_id,
        objective="Summarize trusted research",
        questions=[question],
    )
    lease = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
    assert lease is not None
    stages = (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
    )
    source = Source(source_type=SourceType.WEB, title="Credible research", url="https://example.com/one")
    for current in stages[: stages.index(stage) + 1]:
        previous = (await repo.get(run.run_id)).status
        await runs.advance_run(run.task_id, run.run_id, current, lease=lease)
        assert previous != current
        await runs.start_stage(run.task_id, run.run_id, current, lease=lease)
        if with_artifacts:
            if current == ResearchRunStatus.PLANNING:
                await plans.save_guarded(plan, lease, run_repository=repo)
            elif current == ResearchRunStatus.SEARCHING:
                source.metadata["search"] = {
                    "hits": [
                        {
                            "run_id": str(run.run_id),
                            "question_id": str(question.question_id),
                            "query": question.question,
                            "provider": "test",
                        }
                    ]
                }
                aggregate.add_source(source)
                await research.save_guarded(aggregate, lease, current, run_repository=repo)
            elif current == ResearchRunStatus.EXTRACTING:
                await documents.save_guarded(
                    RetrievedDocument.succeeded(
                        source_id=source.source_id,
                        requested_url=str(source.url),
                        final_url=str(source.url),
                        content="Verified article content",
                    ),
                    lease,
                    task_id=run.task_id,
                    run_repository=repo,
                    research_repository=research,
                )
        await runs.finish_stage(run.task_id, run.run_id, current, lease=lease, outcome=ResearchStageOutcome.COMPLETED)

    inspector = ResearchSafeResumeInspector(runs, research, plans, documents)
    return aggregate, run, runs, repo, research, plans, documents, inspector, lease, plan, source


async def wait_idle(executor):
    async def poll():
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


@pytest.mark.parametrize(
    ("stage", "next_stage"),
    [
        (ResearchRunStatus.PLANNING, ResearchRunStatus.SEARCHING),
        (ResearchRunStatus.SEARCHING, ResearchRunStatus.EXTRACTING),
        (ResearchRunStatus.EXTRACTING, ResearchRunStatus.VERIFYING),
    ],
)
def test_read_only_inspection_and_explicit_resume_skips_completed_stage(stage, next_stage):
    async def run_test():
        _, run, runs, repo, _, _, _, inspector, old, _, _ = await fixture(stage=stage)
        previous = await repo.get(run.run_id)
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.READY
        assert decision.next_stage == next_stage
        assert len(decision.proof_digest) == 64
        assert await repo.get(run.run_id) == previous
        assert not await ResearchRunExecutor(runs, RecordingRunner(), resume_inspector=inspector).resume(
            run.task_id, run.run_id
        )  # Live owner cannot be preempted.
        assert await runs.release_run_lease(old)

        runner = RecordingRunner()
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert await executor.resume(run.task_id, run.run_id)
        await wait_idle(executor)
        assert runner.calls[0] == next_stage
        assert stage not in runner.calls
        assert (await runs.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.COMPLETED
        attempts = await repo.list_stage_attempts(run.run_id)
        assert len([a for a in attempts if a.stage == stage]) == 1
        assert next(a for a in attempts if a.stage == next_stage).fencing_token == old.fencing_token + 1
        assert not await executor.resume(run.task_id, run.run_id)
        await executor.shutdown()

    asyncio.run(run_test())


def test_completed_journal_without_durable_artifact_remains_paused():
    async def run_test():
        _, run, runs, repo, _, _, _, inspector, old, _, _ = await fixture(with_artifacts=False)
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert "plan" in decision.reason
        assert await runs.release_run_lease(old)
        runner = RecordingRunner()
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        assert runner.calls == []
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING
        await executor.shutdown()

    asyncio.run(run_test())


def test_crash_after_side_effect_before_journal_finish_is_not_replayed():
    async def run_test():
        _, run, runs, repo, research, plans, docs, _, lease, plan, _ = await fixture()
        # Simulate a crash after claiming a fresh generation, with durable
        # plan still present but an unfinished duplicate stage attempt.
        assert await runs.release_run_lease(lease)
        replacement = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
        assert replacement is not None
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=replacement)
        await runs.release_run_lease(replacement)
        inspector = ResearchSafeResumeInspector(runs, research, plans, docs)
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert "duplicate" in decision.reason
        runner = RecordingRunner()
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        assert runner.calls == []
        await executor.shutdown()

    asyncio.run(run_test())


@pytest.mark.parametrize("stage", [ResearchRunStatus.VERIFYING, ResearchRunStatus.WRITING])
def test_no_proven_complete_evidence_manifest_or_report_pauses(stage):
    async def run_test():
        _, run, runs, _, _, _, _, inspector, lease, _, _ = await fixture(stage=stage)
        assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.PAUSED
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_search_requires_all_questions_for_this_run_not_just_task_sources():
    async def run_test():
        aggregate, run, runs, repo, research, _, _, inspector, lease, _, source = await fixture(
            stage=ResearchRunStatus.SEARCHING
        )
        aggregate.sources[source.source_id].metadata["search"]["hits"][0]["run_id"] = str(uuid4())
        await research.save_guarded(aggregate, lease, ResearchRunStatus.SEARCHING, run_repository=repo)
        assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.PAUSED
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_missing_or_wrong_retrieval_blocks_resume():
    async def run_test():
        _, run, runs, _, _, _, documents, inspector, lease, _, source = await fixture(
            stage=ResearchRunStatus.EXTRACTING
        )
        await documents.save(
            RetrievedDocument.failed(
                source_id=source.source_id,
                requested_url=str(source.url),
                error="prior run error",
                metadata={"retrieval_run_id": str(uuid4())},
            )
        )
        assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.PAUSED
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_inflight_claim_expiry_and_stale_worker_are_fenced():
    async def run_test():
        clock = ManualClock()
        _, run, runs, repo, _, _, _, inspector, first, _, _ = await fixture(clock=clock)
        # Still held: inspection may say artifacts are ready, but claim fails.
        runner = RecordingRunner(block_stage=ResearchRunStatus.SEARCHING)
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        clock.advance(31)
        assert await executor.resume(run.task_id, run.run_id)
        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        current = (await repo.list_stage_attempts(run.run_id))[-1]
        assert current.fencing_token > first.fencing_token
        with pytest.raises(ResearchRunLeaseLostError):
            await runs.finish_stage(
                run.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=first,
                outcome=ResearchStageOutcome.COMPLETED,
            )
        runner.release.set()
        await wait_idle(executor)
        await executor.shutdown()

    asyncio.run(run_test())


def test_changed_artifact_proof_after_claim_releases_lease_without_dispatch():
    async def run_test():
        _, run, runs, repo, _, _, _, inspector, old, _, _ = await fixture()
        assert await runs.release_run_lease(old)
        original_inspect = inspector.inspect
        calls = 0

        async def changed(task_id, run_id):
            nonlocal calls
            calls += 1
            result = await original_inspect(task_id, run_id)
            if calls == 2:
                return result.__class__(
                    result.task_id,
                    result.run_id,
                    result.status,
                    result.disposition,
                    result.next_stage,
                    result.reason,
                    "different-proof",
                )
            return result

        inspector.inspect = changed
        runner = RecordingRunner()
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        assert calls == 2 and runner.calls == []
        assert (await repo.get(run.run_id)).status == ResearchRunStatus.PLANNING
        # Successfully released the post-check generation, so later claim works.
        another = await repo.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
        assert another is not None
        assert await repo.release_lease(another)
        await executor.shutdown()

    asyncio.run(run_test())


def test_terminal_pending_foreign_and_unconfigured_do_not_resume():
    async def run_test():
        aggregate, run, runs, repo, research, plans, documents, inspector, lease, _, _ = await fixture()
        assert await runs.release_run_lease(lease)
        fresh = await runs.create_run(run.task_id)
        assert (await inspector.inspect(run.task_id, fresh.run_id)).disposition == ResearchResumeDisposition.PENDING
        terminal = await runs.create_run(run.task_id)
        await runs.fail_run(run.task_id, terminal.run_id, "aborted")
        assert (await inspector.inspect(run.task_id, terminal.run_id)).disposition == ResearchResumeDisposition.TERMINAL
        executor = ResearchRunExecutor(runs, RecordingRunner(), resume_inspector=inspector)
        assert not await executor.resume(run.task_id, fresh.run_id)
        with pytest.raises(ResearchRunNotFoundError):
            await executor.resume(uuid4(), run.run_id)
        with pytest.raises(RuntimeError, match="inspector"):
            await ResearchRunExecutor(runs, RecordingRunner()).resume(run.task_id, run.run_id)
        await executor.shutdown()

    asyncio.run(run_test())


def test_artifact_saved_but_stage_attempt_still_started_must_not_replay():
    async def run_test():
        aggregate = ResearchAggregate.create("Interrupted journal commit")
        research = InMemoryResearchRepository()
        repo = InMemoryResearchRunRepository()
        plans = InMemoryResearchPlanRepository()
        documents = InMemoryRetrievedDocumentRepository()
        await research.save(aggregate)
        runs = ResearchRunApplicationService(research, repo)
        run = await runs.create_run(aggregate.task.task_id)
        lease = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
        assert lease is not None
        await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        await plans.save_guarded(
            ResearchPlan(
                task_id=run.task_id,
                run_id=run.run_id,
                objective="Saved before interruption",
                questions=[ResearchQuestion(question="Why was the journal not finished?")],
            ),
            lease,
            run_repository=repo,
        )
        await runs.release_run_lease(lease)
        inspector = ResearchSafeResumeInspector(runs, research, plans, documents)
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert "in-doubt" in decision.reason
        executor = ResearchRunExecutor(runs, RecordingRunner(), resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        await executor.shutdown()

    asyncio.run(run_test())


def test_two_concurrent_explicit_workers_only_one_runs():
    async def run_test():
        _, run, runs, repo, research, plans, docs, inspector, old, _, _ = await fixture()
        assert await runs.release_run_lease(old)
        left_runner = RecordingRunner(block_stage=ResearchRunStatus.SEARCHING)
        right_runner = RecordingRunner(block_stage=ResearchRunStatus.SEARCHING)
        left = ResearchRunExecutor(runs, left_runner, resume_inspector=inspector)
        right = ResearchRunExecutor(runs, right_runner, resume_inspector=inspector)
        results = await asyncio.gather(left.resume(run.task_id, run.run_id), right.resume(run.task_id, run.run_id))
        assert sorted(results) == [False, True]
        winner = left if results[0] else right
        runner = left_runner if results[0] else right_runner
        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        assert len(left_runner.calls) + len(right_runner.calls) == 1
        runner.release.set()
        await wait_idle(winner)
        await left.shutdown()
        await right.shutdown()

    asyncio.run(run_test())


def test_explicit_resume_uses_real_pipeline_services_and_fenced_saves():
    """Restart after durable planning without calling Planner a second time."""
    from enterprise_research.application.pipeline import ResearchPipelineStageRunner
    from enterprise_research.application.planning_service import ResearchPlanningService
    from enterprise_research.application.retrieval import WebFetchResult
    from enterprise_research.application.retrieval_service import ResearchRetrievalService
    from enterprise_research.application.search import SearchResult
    from enterprise_research.application.search_service import ResearchSearchService

    class ForbiddenPlanner:
        async def plan(self, **kwargs):
            raise AssertionError("Planning was already durably completed; must not regenerate")

    class SearchProvider:
        def __init__(self):
            self.calls = []

        async def search(self, query, *, max_results):
            self.calls.append(query)
            return [
                SearchResult(
                    title="Source found after recovery",
                    url="https://example.com/restarted",
                    provider="recovery-test",
                    snippet="Relevant web hit",
                )
            ]

    class Fetcher:
        def __init__(self):
            self.calls = []

        async def fetch(self, url):
            self.calls.append(url)
            return WebFetchResult(
                requested_url=url,
                final_url=url,
                content="Document fetched after safe resume",
            )

    async def run_test():
        _, run, runs, repo, research, plans, documents, inspector, old, _, _ = await fixture()
        assert await runs.release_run_lease(old)
        provider, fetcher = SearchProvider(), Fetcher()
        runner = ResearchPipelineStageRunner(
            ResearchPlanningService(research, repo, plans, ForbiddenPlanner()),
            ResearchSearchService(research, repo, plans, provider),
            ResearchRetrievalService(research, repo, documents, fetcher),
        )
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert await executor.resume(run.task_id, run.run_id)
        await wait_idle(executor)
        assert provider.calls == ["What changed?"]
        assert fetcher.calls == ["https://example.com/restarted"]
        latest = await research.get(run.task_id)
        assert latest is not None and len(latest.sources) == 1
        source = next(iter(latest.sources.values()))
        persisted = await documents.get_for_source(source.source_id)
        assert persisted is not None and persisted.content == "Document fetched after safe resume"
        # The pipeline has no verifying/writing handlers. A completed search
        # and retrieval cannot be mislabeled as a finished research report.
        failed = await repo.get(run.run_id)
        assert failed.status == ResearchRunStatus.FAILED
        assert "VERIFYING requires" in failed.error
        await executor.shutdown()

    asyncio.run(run_test())
