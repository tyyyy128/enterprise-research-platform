"""5B: both resume workers finish their initial proof before racing for lease."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from enterprise_research.application import ResearchRunApplicationService, ResearchRunExecutor, ResearchStageOutcome
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


class TwoPreflights:
    """Latch the first two reads, but not the post-claim re-inspection."""

    def __init__(self, inspector):
        self._inspector = inspector
        self.initial = 0
        self.both_ready = asyncio.Event()

    async def inspect(self, task_id, run_id):
        decision = await self._inspector.inspect(task_id, run_id)
        if self.initial < 2:
            self.initial += 1
            if self.initial == 2:
                self.both_ready.set()
            await asyncio.wait_for(self.both_ready.wait(), timeout=3)
        return decision


class BlockingStage:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run_stage(self, _task_id, _run_id, stage, *, lease=None):
        assert lease is not None
        self.calls.append(stage)
        if stage == ResearchRunStatus.SEARCHING:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=3)


def test_two_workers_with_identical_initial_proofs_dispatch_only_one_stage():
    """A deterministic preclaim race, not a sequence of two resume() calls."""

    async def exercise():
        aggregate = ResearchAggregate.create("Two simultaneous preclaim proofs")
        research = InMemoryResearchRepository()
        repository = InMemoryResearchRunRepository()
        plans = InMemoryResearchPlanRepository()
        docs = InMemoryRetrievedDocumentRepository()
        await research.save(aggregate)
        runs = ResearchRunApplicationService(research, repository)
        run = await runs.create_run(aggregate.task.task_id)
        old = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
        assert old is not None
        await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
        await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=old)
        await plans.save_guarded(
            ResearchPlan(
                task_id=run.task_id,
                run_id=run.run_id,
                objective="Make the proof stable",
                questions=[ResearchQuestion(question="Who has the fenced lease?")],
            ),
            old,
            run_repository=repository,
        )
        await runs.finish_stage(
            run.task_id,
            run.run_id,
            ResearchRunStatus.PLANNING,
            lease=old,
            outcome=ResearchStageOutcome.COMPLETED,
        )
        assert await runs.release_run_lease(old)

        inspector = ResearchSafeResumeInspector(runs, research, plans, docs)
        assert (await inspector.inspect(run.task_id, run.run_id)).disposition == ResearchResumeDisposition.READY
        gate = TwoPreflights(inspector)
        runners = [BlockingStage(), BlockingStage()]
        executors = [ResearchRunExecutor(runs, runner, resume_inspector=gate) for runner in runners]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(executor.resume(run.task_id, run.run_id) for executor in executors)), timeout=4
            )
            assert gate.initial == 2
            assert sorted(results) == [False, True]
            assert executors[0]._owner_id != executors[1]._owner_id
            await asyncio.wait_for(runners[results.index(True)].entered.wait(), timeout=2)
            assert [runner.calls.count(ResearchRunStatus.SEARCHING) for runner in runners] in ([1, 0], [0, 1])
            attempts = await runs.inspect_stage_attempts(run.task_id, run.run_id)
            assert len([attempt for attempt in attempts if attempt.stage == ResearchRunStatus.SEARCHING]) == 1
        finally:
            for runner in runners:
                runner.release.set()
            for executor in executors:
                await executor.shutdown()

    asyncio.run(exercise())
