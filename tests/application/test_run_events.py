"""Unit tests for worker-local research run event replay and isolation."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.api.sse import encode_run_event
from enterprise_research.application.evidence_progress import EvidenceExtractionProgress
from enterprise_research.application.run_events import (
    ResearchRunEventBus,
    RunEventHistoryExpired,
)
from enterprise_research.domain import ResearchRunStatus


def make_progress(task_id, run_id, completed: int) -> EvidenceExtractionProgress:
    return EvidenceExtractionProgress(
        task_id=task_id,
        run_id=run_id,
        eligible_document_count=1,
        selected_document_count=1,
        available_chunk_count=2,
        scheduled_chunk_count=2,
        skipped_chunk_count=0,
        completed_chunk_count=completed,
        candidate_count=completed,
        verified_count=completed,
        rejected_count=0,
        deduplicated_count=0,
        created_count=completed,
        reused_count=0,
    )


def test_run_events_replay_in_sequence_and_stop_on_completion() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.VERIFYING)
        await bus.publish_evidence_progress(make_progress(task_id, run_id, 0))
        await bus.publish_evidence_progress(make_progress(task_id, run_id, 2))
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.COMPLETED)

        events = [event async for event in bus.stream(task_id, run_id)]
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
        assert [event.event for event in events] == [
            "run.created",
            "run.stage",
            "evidence.progress",
            "evidence.progress",
            "run.completed",
        ]
        assert events[2].data["completed_chunk_count"] == 0
        assert events[3].data["completed_chunk_count"] == 2
        assert events[3].data["remaining_chunk_count"] == 0
        assert events[3].data["completion_ratio"] == 1.0
        assert events[3].data["run_id"] == str(run_id)

        resumed = [event async for event in bus.stream(task_id, run_id, last_event_id=3)]
        assert [event.sequence for event in resumed] == [4, 5]
        assert [event async for event in bus.stream(task_id, run_id, last_event_id=5)] == []
        await bus.publish_failure(task_id, run_id, "too late")
        assert [event async for event in bus.stream(task_id, run_id)] == events

    asyncio.run(run())


def test_expired_history_is_reported_before_streaming_and_to_slow_subscribers() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus(history_limit=2)
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        subscriber = bus.stream(task_id, run_id)
        first = await anext(subscriber)
        assert first is not None and first.sequence == 1

        await bus.publish_stage(task_id, run_id, ResearchRunStatus.PLANNING)
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.SEARCHING)
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.EXTRACTING)

        with pytest.raises(RunEventHistoryExpired):
            bus.assert_available(task_id, run_id, 1)

        gap = await anext(subscriber)
        assert gap is not None and gap.event == "stream.reset"
        assert gap.sequence is None
        assert gap.data["reason"] == "history_expired"
        assert encode_run_event(gap).startswith("event: stream.reset\n")
        with pytest.raises(StopAsyncIteration):
            await anext(subscriber)

    asyncio.run(run())


def test_event_bus_is_task_scoped_and_rejects_future_cursors() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        with pytest.raises(ValueError, match="different task"):
            bus.register_run(uuid4(), run_id)
        with pytest.raises(LookupError):
            bus.assert_available(uuid4(), run_id, None)
        with pytest.raises(ValueError, match="newer"):
            bus.assert_available(task_id, run_id, 99)
        with pytest.raises(ValueError):
            ResearchRunEventBus(history_limit=0)

    asyncio.run(run())


def test_heartbeat_and_disconnect_do_not_cancel_run_events() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus(heartbeat_seconds=0.01)
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        subscriber = bus.stream(task_id, run_id)
        assert (await anext(subscriber)).event == "run.created"
        assert await asyncio.wait_for(anext(subscriber), timeout=0.5) is None
        await subscriber.aclose()

        await bus.publish_stage(task_id, run_id, ResearchRunStatus.COMPLETED)
        assert [event.event async for event in bus.stream(task_id, run_id)] == [
            "run.created",
            "run.completed",
        ]

    asyncio.run(run())


def test_finished_run_retention_is_bounded_without_eviction_of_active_runs() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus(completed_run_limit=1)
        task_id, active, finished_a, finished_b = uuid4(), uuid4(), uuid4(), uuid4()
        for run_id in (active, finished_a, finished_b):
            bus.register_run(task_id, run_id)
        await bus.publish_stage(task_id, finished_a, ResearchRunStatus.COMPLETED)
        await bus.publish_stage(task_id, finished_b, ResearchRunStatus.COMPLETED)
        with pytest.raises(LookupError):
            bus.assert_available(task_id, finished_a, None)
        bus.assert_available(task_id, active, None)
        bus.assert_available(task_id, finished_b, None)

    asyncio.run(run())


def test_sse_frame_escapes_json_and_keeps_heartbeat_unsequenced() -> None:
    async def run() -> None:
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        await bus.publish_failure(task_id, run_id, "unexpected\nreason: ü")
        events = [event async for event in bus.stream(task_id, run_id)]
        frame = encode_run_event(events[-1])
        assert frame.startswith("id: 2\nevent: run.failed\ndata: {")
        assert "\\nreason: ü" in frame
        assert frame.endswith("\n\n")
        assert encode_run_event(None) == ": keep-alive\n\n"

    asyncio.run(run())
