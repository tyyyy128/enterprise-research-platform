"""4B: failure-window matrix for advisory Graph / authoritative Stage Journal."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchCheckpointObservation,
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointReference,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchSafeResumeInspector,
    ResearchStageOutcome,
    reconcile_stage_checkpoint,
)
from enterprise_research.application.safe_resume import ResearchResumeDisposition
from enterprise_research.domain import ResearchAggregate, ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


class TestCheckpointReader:
    """Reference-only test double, not a LangGraph Saver or business writer."""

    __test__ = False

    def __init__(self, mode="absent"):
        self.mode = mode
        self.calls = []

    async def latest(self, binding):
        self.calls.append(binding)
        if self.mode == "absent":
            return None
        if self.mode == "observed":
            return ResearchGraphCheckpointReference(binding, "checkpoint-A")
        if self.mode == "changed":
            return ResearchGraphCheckpointReference(binding, "checkpoint-B")
        if self.mode == "mismatched":
            other = ResearchGraphCheckpointBinding(uuid4(), binding.run_id, binding.stage, binding.fencing_token)
            return ResearchGraphCheckpointReference(other, "foreign-checkpoint")
        if self.mode == "malformed":
            return object()
        if self.mode == "outage":
            raise RuntimeError("SECRET graph-store backend details should never appear in a resume reason")
        raise AssertionError(self.mode)


async def prepare(*, outcome=ResearchStageOutcome.COMPLETED, has_plan=True, reader=None):
    aggregate = ResearchAggregate.create("4B failure-window reconciliation")
    research = InMemoryResearchRepository()
    repository = InMemoryResearchRunRepository()
    plans = InMemoryResearchPlanRepository()
    documents = InMemoryRetrievedDocumentRepository()
    await research.save(aggregate)
    runs = ResearchRunApplicationService(research, repository)
    run = await runs.create_run(aggregate.task.task_id)
    lease = await runs.claim_run_lease(run.task_id, run.run_id, uuid4(), ttl_seconds=30)
    assert lease is not None
    await runs.advance_run(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
    await runs.start_stage(run.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
    if has_plan:
        await plans.save_guarded(
            ResearchPlan(
                task_id=run.task_id,
                run_id=run.run_id,
                objective="Check the business result",
                questions=[ResearchQuestion(question="What was committed?")],
            ),
            lease,
            run_repository=repository,
        )
    if outcome != ResearchStageOutcome.STARTED:
        await runs.finish_stage(
            run.task_id,
            run.run_id,
            ResearchRunStatus.PLANNING,
            lease=lease,
            outcome=outcome,
            error="Failed after checkpoint" if outcome == ResearchStageOutcome.FAILED else None,
        )
    inspector = ResearchSafeResumeInspector(runs, research, plans, documents, graph_checkpoints=reader)
    return run, lease, runs, repository, inspector


@pytest.mark.parametrize(
    ("mode", "outcome", "has_plan", "expected", "presence", "reason"),
    [
        # Graph checkpoint ahead of journal: neither checkpoint nor artifact can close an in-doubt attempt.
        ("observed", ResearchStageOutcome.STARTED, True, "paused", "present", "in-doubt"),
        ("absent", ResearchStageOutcome.STARTED, True, "paused", "absent", "in-doubt"),
        ("observed", ResearchStageOutcome.FAILED, True, "paused", "present", "failed"),
        # Journal/Plan ahead of Graph checkpoint: missing Graph snapshot does not block safe artifact checks.
        ("absent", ResearchStageOutcome.COMPLETED, True, "ready", "absent", "Completed planning"),
        ("observed", ResearchStageOutcome.COMPLETED, True, "ready", "present", "Completed planning"),
        # A checkpoint is NEVER a substitute for the durable business artifact.
        ("observed", ResearchStageOutcome.COMPLETED, False, "paused", "present", "plan"),
        ("absent", ResearchStageOutcome.COMPLETED, False, "paused", "absent", "plan"),
        # Identity and observation failures are not equivalent to absent Graph data.
        ("mismatched", ResearchStageOutcome.COMPLETED, True, "paused", "identity_conflict", "checkpoint"),
        ("malformed", ResearchStageOutcome.COMPLETED, True, "paused", "identity_conflict", "checkpoint"),
        ("outage", ResearchStageOutcome.COMPLETED, True, "paused", "unavailable", "unavailable"),
    ],
)
def test_checkpoint_stage_journal_failure_window_matrix(mode, outcome, has_plan, expected, presence, reason):
    async def run_test():
        reader = TestCheckpointReader(mode)
        run, lease, runs, repository, inspector = await prepare(outcome=outcome, has_plan=has_plan, reader=reader)
        decision = await inspector.inspect(run.task_id, run.run_id)
        assert decision.disposition == (
            ResearchResumeDisposition.READY if expected == "ready" else ResearchResumeDisposition.PAUSED
        )
        assert reason.lower() in decision.reason.lower()
        assert len(decision.checkpoint_reconciliations) == 1
        check = decision.checkpoint_reconciliations[0]
        assert check.checkpoint == getattr(ResearchCheckpointObservation, presence.upper())
        assert check.may_verify_artifacts == (
            outcome == ResearchStageOutcome.COMPLETED and mode in {"absent", "observed"}
        )
        assert check.fencing_token == lease.fencing_token
        assert reader.calls == [
            ResearchGraphCheckpointBinding.for_lease(run.task_id, ResearchRunStatus.PLANNING, lease)
        ]
        assert (await repository.get(run.run_id)).status == ResearchRunStatus.PLANNING
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_unconfigured_observer_remains_optional_but_outage_fails_closed(caplog):
    async def run_test():
        run, lease, runs, _, inspector = await prepare(reader=None)
        valid = await inspector.inspect(run.task_id, run.run_id)
        assert valid.disposition == ResearchResumeDisposition.READY
        assert valid.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.NOT_CONFIGURED
        reader = TestCheckpointReader("outage")
        inspector._graph_checkpoints = reader
        blocked = await inspector.inspect(run.task_id, run.run_id)
        assert blocked.disposition == ResearchResumeDisposition.PAUSED
        assert blocked.proof_digest is None
        assert "SECRET" not in blocked.reason
        assert valid.proof_digest != blocked.proof_digest
        await runs.release_run_lease(lease)

    asyncio.run(run_test())
    assert "SECRET" not in caplog.text


def test_journal_missing_or_duplicate_does_not_guess_graph_generation():
    async def run_test():
        run, lease, runs, _, inspector = await prepare(reader=TestCheckpointReader("observed"))
        reader = inspector._graph_checkpoints
        assert reader is not None
        attempts = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        for ambiguous in ([], [attempts[0], replace(attempts[0])]):
            check = await reconcile_stage_checkpoint(
                task_id=run.task_id,
                run_id=run.run_id,
                stage=ResearchRunStatus.PLANNING,
                attempts=ambiguous,
                reader=reader,
            )
            assert check.checkpoint == ResearchCheckpointObservation.NOT_CHECKED
            assert check.fencing_token is None and not check.may_verify_artifacts
            assert check.pause_reason and "duplicate" in check.pause_reason
        assert reader.calls == []
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_checkpoint_appears_or_disappears_in_proof_even_if_journal_and_plan_are_unchanged():
    async def run_test():
        reader = TestCheckpointReader("absent")
        run, lease, runs, _, inspector = await prepare(reader=reader)
        absent = await inspector.inspect(run.task_id, run.run_id)
        reader.mode = "observed"
        present = await inspector.inspect(run.task_id, run.run_id)
        reader.mode = "changed"
        changed = await inspector.inspect(run.task_id, run.run_id)
        reader.mode = "absent"
        absent_again = await inspector.inspect(run.task_id, run.run_id)
        assert absent.proof_digest != present.proof_digest
        assert changed.proof_digest != present.proof_digest
        assert absent.proof_digest == absent_again.proof_digest
        assert all(d.disposition == ResearchResumeDisposition.READY for d in (absent, present, changed))
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


@pytest.mark.parametrize("after_claim", ["outage", "absent", "mismatched", "changed"])
def test_changed_or_unavailable_checkpoint_after_claim_releases_lease_and_never_dispatches(after_claim):
    async def run_test():
        reader = TestCheckpointReader("observed")
        run, lease, runs, repository, inspector = await prepare(reader=reader)
        await runs.release_run_lease(lease)
        original_claim = runs.claim_reconciled_run_lease

        async def claim_then_flip(*args, **kwargs):
            newly_claimed = await original_claim(*args, **kwargs)
            assert newly_claimed is not None
            reader.mode = after_claim
            return newly_claimed

        runs.claim_reconciled_run_lease = claim_then_flip

        class ForbiddenRunner:
            async def run_stage(self, *args, **kwargs):
                raise AssertionError("A changed checkpoint must never dispatch stage work")

        executor = ResearchRunExecutor(runs, ForbiddenRunner(), resume_inspector=inspector)
        assert not await executor.resume(run.task_id, run.run_id)
        assert executor.active_count == 0
        assert (await repository.get(run.run_id)).status == ResearchRunStatus.PLANNING
        # A fresh generation can be acquired: the cancelled recovery released its own lease.
        fresh = await repository.claim_lease(run.run_id, uuid4(), ttl_seconds=30, allow_inflight=True)
        assert fresh is not None
        assert fresh.fencing_token == lease.fencing_token + 2
        assert await repository.release_lease(fresh)
        await executor.shutdown()

    asyncio.run(run_test())


def test_checkpoint_backend_failure_through_real_aget_tuple_adapter_pauses():
    async def run_test():
        from enterprise_research.application import LangGraphCheckpointReader

        class BrokenSaver:
            async def aget_tuple(self, config):
                raise OSError("SECRET backend error payload")

        run, lease, runs, _, inspector = await prepare(reader=LangGraphCheckpointReader(BrokenSaver()))
        result = await inspector.inspect(run.task_id, run.run_id)
        assert result.disposition == ResearchResumeDisposition.PAUSED
        assert result.checkpoint_reconciliations[0].checkpoint == ResearchCheckpointObservation.UNAVAILABLE
        assert "SECRET" not in result.reason
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_journal_attempt_from_different_run_fails_closed_before_graph_read():
    async def run_test():
        reader = TestCheckpointReader("observed")
        run, lease, runs, _, _ = await prepare(reader=reader)
        history = await runs.inspect_stage_attempts(run.task_id, run.run_id)
        wrong = replace(history[0], run_id=uuid4())
        result = await reconcile_stage_checkpoint(
            task_id=run.task_id,
            run_id=run.run_id,
            stage=ResearchRunStatus.PLANNING,
            attempts=[wrong],
            reader=reader,
        )
        assert result.checkpoint == ResearchCheckpointObservation.JOURNAL_SCOPE_CONFLICT
        assert not result.may_verify_artifacts
        assert "different Run" in result.pause_reason
        assert reader.calls == []
        await runs.release_run_lease(lease)

    asyncio.run(run_test())


def test_checkpoint_read_cancellation_is_not_converted_into_absence():
    async def run_test():
        reader = TestCheckpointReader("observed")
        run, lease, runs, _, _ = await prepare(reader=reader)
        attempts = await runs.inspect_stage_attempts(run.task_id, run.run_id)

        class CancellingReader:
            async def latest(self, binding):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await reconcile_stage_checkpoint(
                task_id=run.task_id,
                run_id=run.run_id,
                stage=ResearchRunStatus.PLANNING,
                attempts=attempts,
                reader=CancellingReader(),
            )
        await runs.release_run_lease(lease)

    asyncio.run(run_test())
