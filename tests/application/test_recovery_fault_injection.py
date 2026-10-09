"""5A: deterministic fault injection around the journal/business commit gap.

These tests run without PostgreSQL or a model provider. Cancelling the task
models cooperative shutdown, NOT SIGKILL; subprocess SIGKILL is covered by the
separately opted-in PostgreSQL tests.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application import ResearchRunApplicationService, ResearchRunExecutor, ResearchStageOutcome
from enterprise_research.application.checkpoint_consistency import ResearchCheckpointObservation
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


class BlockingPlanning:
    """Inject shutdown strictly after a selected durable memory side effect."""

    def __init__(self, *, write_plan, plans, runs_repo):
        self.write_plan = write_plan
        self.plans = plans
        self.runs_repo = runs_repo
        self.entered = asyncio.Event()
        self.calls = []

    async def run_stage(self, task_id, run_id, stage, *, lease=None):
        self.calls.append(stage)
        assert stage == ResearchRunStatus.PLANNING
        assert lease is not None
        if self.write_plan:
            await self.plans.save_guarded(
                ResearchPlan(
                    task_id=task_id,
                    run_id=run_id,
                    objective="5A test: saved before interruption",
                    questions=[ResearchQuestion(question="What was committed?")],
                ),
                lease,
                run_repository=self.runs_repo,
            )
        self.entered.set()
        await asyncio.Event().wait()  # The controlled executor.shutdown() cancels here.


@pytest.mark.parametrize("write_plan", [False, True], ids=["after-journal-entry", "after-business-artifact"])
def test_cancellation_leaves_started_journal_and_refuses_unproven_replay(write_plan):
    async def exercise():
        aggregate = ResearchAggregate.create("5A cancellation boundary")
        research = InMemoryResearchRepository()
        runs_repo = InMemoryResearchRunRepository()
        plans = InMemoryResearchPlanRepository()
        docs = InMemoryRetrievedDocumentRepository()
        await research.save(aggregate)
        app = ResearchRunApplicationService(research, runs_repo)
        run = await app.create_run(aggregate.task.task_id)
        inspector = ResearchSafeResumeInspector(app, research, plans, docs)
        runner = BlockingPlanning(write_plan=write_plan, plans=plans, runs_repo=runs_repo)
        executor = ResearchRunExecutor(app, runner, resume_inspector=inspector)
        try:
            executor.start(run.task_id, run.run_id)
            await asyncio.wait_for(runner.entered.wait(), timeout=3)
        finally:
            await executor.shutdown()

        persisted = await app.get_run(run.task_id, run.run_id)
        attempts = await app.inspect_stage_attempts(run.task_id, run.run_id)
        assert persisted.status == ResearchRunStatus.PLANNING
        assert len(attempts) == 1
        assert attempts[0].outcome == ResearchStageOutcome.STARTED
        assert (await plans.get_for_run(run.run_id) is not None) is write_plan
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert decision.next_stage is None
        assert decision.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.NOT_CONFIGURED

        replacement = BlockingPlanning(write_plan=False, plans=plans, runs_repo=runs_repo)
        recovering = ResearchRunExecutor(app, replacement, resume_inspector=inspector)
        try:
            assert not await recovering.resume(run.task_id, run.run_id)
            assert replacement.calls == []
            assert len(await app.inspect_stage_attempts(run.task_id, run.run_id)) == 1
        finally:
            await recovering.shutdown()

    asyncio.run(exercise())


def test_checkpoint_read_failure_after_complete_journal_still_pauses():
    class BrokenCheckpointReader:
        async def latest(self, _binding):
            raise ConnectionError("injected: checkpoint store unavailable")

    async def exercise():
        aggregate = ResearchAggregate.create("5A checkpoint outage after journal completion")
        research = InMemoryResearchRepository()
        runs_repo = InMemoryResearchRunRepository()
        plans = InMemoryResearchPlanRepository()
        docs = InMemoryRetrievedDocumentRepository()
        await research.save(aggregate)
        app = ResearchRunApplicationService(research, runs_repo)
        run = await app.create_run(aggregate.task.task_id)
        lease = await app.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
        assert lease is not None
        await app.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        await app.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        await plans.save_guarded(
            ResearchPlan(
                task_id=run.task_id,
                run_id=run.run_id,
                objective="Durable plan exists but checkpoint store failed",
                questions=[ResearchQuestion(question="Is the checkpoint store up?")],
            ),
            lease,
            run_repository=runs_repo,
        )
        await app.finish_stage(
            run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.COMPLETED
        )
        assert await app.release_run_lease(lease)

        inspector = ResearchSafeResumeInspector(app, research, plans, docs, graph_checkpoints=BrokenCheckpointReader())
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert decision.next_stage is None
        assert decision.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.UNAVAILABLE

        replacement = BlockingPlanning(write_plan=False, plans=plans, runs_repo=runs_repo)
        executor = ResearchRunExecutor(app, replacement, resume_inspector=inspector)
        try:
            assert not await executor.resume(run.task_id, run.run_id)
            assert replacement.calls == []
            assert (await app.get_run(run.task_id, run.run_id)).status == ResearchRunStatus.PLANNING
        finally:
            await executor.shutdown()

    asyncio.run(exercise())
