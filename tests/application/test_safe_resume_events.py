"""Event history for explicit resume is distinct from a newly created run."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application.run_events import ResearchRunEventBus
from enterprise_research.domain import ResearchRunStatus


def test_fresh_resume_uses_resume_event_not_fake_run_created():
    async def exercise():
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        await bus.publish_resume(
            task_id, run_id, previous_stage=ResearchRunStatus.PLANNING, next_stage=ResearchRunStatus.SEARCHING
        )
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.COMPLETED)
        events = [item async for item in bus.stream(task_id, run_id)]
        assert [event.event for event in events] == ["run.resumed", "run.completed"]
        assert [event.sequence for event in events] == [1, 2]
        assert events[0].data["previous_stage"] == "planning"
        assert events[0].data["next_stage"] == "searching"

    asyncio.run(exercise())


def test_interrupted_local_stream_can_continue_with_monotonic_event_ids():
    async def exercise():
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        await bus.publish_interruption(task_id, run_id)
        await bus.publish_resume(
            task_id, run_id, previous_stage=ResearchRunStatus.PLANNING, next_stage=ResearchRunStatus.SEARCHING
        )
        await bus.publish_stage(task_id, run_id, ResearchRunStatus.COMPLETED)
        events = [item async for item in bus.stream(task_id, run_id)]
        assert [event.event for event in events] == ["run.created", "run.interrupted", "run.resumed", "run.completed"]
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert [item.event async for item in bus.stream(task_id, run_id, last_event_id=2)] == [
            "run.resumed",
            "run.completed",
        ]

    asyncio.run(exercise())


def test_terminal_success_or_failure_stream_is_not_reopened():
    async def exercise():
        bus = ResearchRunEventBus()
        task_id, run_id = uuid4(), uuid4()
        bus.register_run(task_id, run_id)
        await bus.publish_failure(task_id, run_id, "no result")
        with pytest.raises(RuntimeError, match="completed or failed"):
            await bus.publish_resume(
                task_id, run_id, previous_stage=ResearchRunStatus.PLANNING, next_stage=ResearchRunStatus.SEARCHING
            )

    asyncio.run(exercise())
