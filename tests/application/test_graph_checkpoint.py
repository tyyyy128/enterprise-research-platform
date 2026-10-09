"""4A: checkpoint namespace isolation and advisory Safe Resume observation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from enterprise_research.application import (
    LangGraphCheckpointReader,
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointConflictError,
    ResearchGraphCheckpointReference,
    ResearchRunApplicationService,
    ResearchSafeResumeInspector,
    ResearchStageOutcome,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.safe_resume import ResearchResumeDisposition
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


class FakeCheckpointSaver:
    def __init__(self):
        self.record = None
        self.calls = []

    async def aget_tuple(self, config):
        self.calls.append(config)
        return self.record


def sample_tuple(
    binding, *, thread_id=None, checkpoint_ns=None, config_id="checkpoint-123", payload_id="checkpoint-123"
):
    return SimpleNamespace(
        config={
            "configurable": {
                "thread_id": binding.thread_id if thread_id is None else thread_id,
                "checkpoint_ns": binding.checkpoint_ns if checkpoint_ns is None else checkpoint_ns,
                "checkpoint_id": config_id,
            }
        },
        checkpoint={"id": payload_id, "channel_values": {"sensitive": "do not expose"}},
    )


def sample_binding(*, token=7, stage=ResearchRunStatus.SEARCHING):
    return ResearchGraphCheckpointBinding(uuid4(), uuid4(), stage, token)


def test_checkpoint_binding_is_stable_isolated_and_fenced():
    binding = sample_binding()
    assert binding.runnable_config() == {
        "configurable": {"thread_id": binding.thread_id, "checkpoint_ns": binding.checkpoint_ns}
    }
    assert str(binding.task_id) in binding.thread_id
    assert str(binding.run_id) in binding.thread_id
    assert "searching" in binding.checkpoint_ns
    assert binding == ResearchGraphCheckpointBinding(
        binding.task_id, binding.run_id, binding.stage, binding.fencing_token
    )
    assert binding.runnable_config() == binding.runnable_config()
    assert (
        binding.checkpoint_ns
        != ResearchGraphCheckpointBinding(
            binding.task_id, binding.run_id, binding.stage, binding.fencing_token + 1
        ).checkpoint_ns
    )
    assert (
        binding.checkpoint_ns
        != ResearchGraphCheckpointBinding(
            binding.task_id, binding.run_id, ResearchRunStatus.PLANNING, binding.fencing_token
        ).checkpoint_ns
    )
    assert (
        binding.thread_id
        != ResearchGraphCheckpointBinding(uuid4(), binding.run_id, binding.stage, binding.fencing_token).thread_id
    )
    lease = ResearchRunLease(binding.run_id, uuid4(), binding.fencing_token, datetime.now(UTC) + timedelta(minutes=1))
    assert ResearchGraphCheckpointBinding.for_lease(binding.task_id, binding.stage, lease) == binding


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "7"])
def test_checkpoint_binding_rejects_invalid_fencing_generation(bad):
    with pytest.raises(ValueError):
        sample_binding(token=bad)


@pytest.mark.parametrize("bad", [ResearchRunStatus.PENDING, ResearchRunStatus.COMPLETED, ResearchRunStatus.FAILED])
def test_checkpoint_binding_rejects_nonexecution_stage(bad):
    with pytest.raises(ValueError):
        sample_binding(stage=bad)


def test_checkpoint_reference_cannot_be_blank():
    with pytest.raises(ValueError):
        ResearchGraphCheckpointReference(sample_binding(), "   ")


def test_langgraph_checkpoint_reader_reads_only_identity_not_state():
    async def test():
        binding = sample_binding()
        saver = FakeCheckpointSaver()
        reader = LangGraphCheckpointReader(saver)
        assert await reader.latest(binding) is None
        saver.record = sample_tuple(binding)
        observed = await reader.latest(binding)
        assert observed == ResearchGraphCheckpointReference(binding, "checkpoint-123")
        assert not hasattr(observed, "channel_values")
        assert saver.calls == [binding.runnable_config(), binding.runnable_config()]

    asyncio.run(test())


@pytest.mark.parametrize("wrong", ["thread_id", "checkpoint_ns", "config_id", "payload_id", "no_id", "no_config"])
def test_langgraph_checkpoint_reader_rejects_conflicts(wrong):
    async def test():
        binding = sample_binding()
        saver = FakeCheckpointSaver()
        kwargs = {}
        if wrong == "thread_id":
            kwargs["thread_id"] = "an-other-run"
        elif wrong == "checkpoint_ns":
            kwargs["checkpoint_ns"] = "searching/older-generation"
        elif wrong == "config_id":
            kwargs["config_id"] = "wrong-id"
        elif wrong == "payload_id":
            kwargs["payload_id"] = "wrong-id"
        elif wrong == "no_id":
            kwargs["config_id"] = None
            kwargs["payload_id"] = None
        saver.record = sample_tuple(binding, **kwargs)
        if wrong == "no_config":
            saver.record.config = {}
        with pytest.raises(ResearchGraphCheckpointConflictError):
            await LangGraphCheckpointReader(saver).latest(binding)

    asyncio.run(test())


def test_graph_reader_requires_explicitly_supplied_saver():
    with pytest.raises(TypeError):
        LangGraphCheckpointReader(object())


async def build_planning_run():
    aggregate = ResearchAggregate.create("Recover plan after restart")
    research = InMemoryResearchRepository()
    repository = InMemoryResearchRunRepository()
    plans = InMemoryResearchPlanRepository()
    documents = InMemoryRetrievedDocumentRepository()
    await research.save(aggregate)
    runs = ResearchRunApplicationService(research, repository)
    run = await runs.create_run(aggregate.task.task_id)
    lease = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
    assert lease is not None
    stage = ResearchRunStatus.PLANNING
    await runs.advance_run(run.task_id, run.run_id, stage, lease=lease)
    await runs.start_stage(run.task_id, run.run_id, stage, lease=lease)
    await plans.save_guarded(
        ResearchPlan(
            task_id=run.task_id,
            run_id=run.run_id,
            objective="Investigate",
            questions=[ResearchQuestion(question="What is the evidence?")],
        ),
        lease,
        run_repository=repository,
    )
    await runs.finish_stage(run.task_id, run.run_id, stage, lease=lease, outcome=ResearchStageOutcome.COMPLETED)
    return run, lease, runs, repository, research, plans, documents


def test_checkpoint_is_advisory_not_business_completion_proof():
    async def test():
        run, lease, runs, _, research, plans, documents = await build_planning_run()
        identity = ResearchGraphCheckpointBinding.for_lease(run.task_id, ResearchRunStatus.PLANNING, lease)
        saver = FakeCheckpointSaver()
        observer = LangGraphCheckpointReader(saver)
        inspector = ResearchSafeResumeInspector(runs, research, plans, documents, graph_checkpoints=observer)
        without = await inspector.inspect(run.task_id, run.run_id)
        assert without.disposition == ResearchResumeDisposition.READY
        assert without.next_stage == ResearchRunStatus.SEARCHING
        saver.record = sample_tuple(identity)
        with_checkpoint = await inspector.inspect(run.task_id, run.run_id)
        assert with_checkpoint.disposition == ResearchResumeDisposition.READY
        assert with_checkpoint.proof_digest != without.proof_digest
        # A valid checkpoint is merely advisory; removing the durable Plan still pauses.
        plans._items.clear()  # In-memory test double: simulate missing authoritative artifact.
        no_plan = await inspector.inspect(run.task_id, run.run_id)
        assert no_plan.disposition == ResearchResumeDisposition.PAUSED
        assert "plan" in no_plan.reason
        assert await runs.release_run_lease(lease)

    asyncio.run(test())


def test_checkpoint_mismatch_pauses_without_claiming_or_mutating_run():
    async def test():
        run, lease, runs, repository, research, plans, documents = await build_planning_run()
        binding = ResearchGraphCheckpointBinding.for_lease(run.task_id, ResearchRunStatus.PLANNING, lease)
        before = await repository.get(run.run_id)
        saver = FakeCheckpointSaver()
        saver.record = sample_tuple(binding, checkpoint_ns="bad-generation")
        inspector = ResearchSafeResumeInspector(
            runs, research, plans, documents, graph_checkpoints=LangGraphCheckpointReader(saver)
        )
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert "checkpoint" in decision.reason.lower()
        assert await repository.get(run.run_id) == before
        assert await runs.release_run_lease(lease)

    asyncio.run(test())


def test_untrusted_observer_cannot_return_different_task_binding():
    async def test():
        run, lease, runs, _, research, plans, documents = await build_planning_run()

        class BadReader:
            async def latest(self, binding):
                return ResearchGraphCheckpointReference(
                    ResearchGraphCheckpointBinding(uuid4(), binding.run_id, binding.stage, binding.fencing_token),
                    "fake-checkpoint",
                )

        inspector = ResearchSafeResumeInspector(runs, research, plans, documents, graph_checkpoints=BadReader())
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == ResearchResumeDisposition.PAUSED
        assert await runs.release_run_lease(lease)

    asyncio.run(test())


def test_checkpoint_change_between_preflight_and_claim_releases_lease_without_dispatch():
    async def test():
        from enterprise_research.application import ResearchRunExecutor

        run, old, runs, repository, research, plans, documents = await build_planning_run()
        assert await runs.release_run_lease(old)
        binding = ResearchGraphCheckpointBinding.for_lease(run.task_id, ResearchRunStatus.PLANNING, old)
        saver = FakeCheckpointSaver()
        inspector = ResearchSafeResumeInspector(
            runs, research, plans, documents, graph_checkpoints=LangGraphCheckpointReader(saver)
        )
        claim = runs.claim_reconciled_run_lease

        async def claim_then_checkpoint(*args, **kwargs):
            current = await claim(*args, **kwargs)
            if current is not None:
                saver.record = sample_tuple(binding)
            return current

        runs.claim_reconciled_run_lease = claim_then_checkpoint

        class RecordingStage:
            def __init__(self):
                self.calls = []

            async def run_stage(self, task_id, run_id, stage, *, lease=None):
                self.calls.append(stage)

        runner = RecordingStage()
        executor = ResearchRunExecutor(runs, runner, resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        assert not runner.calls
        assert executor.active_count == 0
        assert (await repository.get(run.run_id)).status == ResearchRunStatus.PLANNING
        # A rejected second proof releases the newly claimed generation.
        fresh = await claim(
            run.task_id, run.run_id, uuid4(), ttl_seconds=30, expected_status=ResearchRunStatus.PLANNING
        )
        assert fresh is not None
        assert fresh.fencing_token == old.fencing_token + 2
        assert await runs.release_run_lease(fresh)
        await executor.shutdown()

    asyncio.run(test())
