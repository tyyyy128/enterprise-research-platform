"""Bounded, process-local research run events for HTTP streaming.

This intentionally is not a durable event store or a cross-worker pub/sub system.
The executor owns a run independently of the number of HTTP subscribers.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from uuid import UUID

from enterprise_research.application.evidence_progress import EvidenceExtractionProgress
from enterprise_research.domain import ResearchRunStatus


class RunEventHistoryExpired(ValueError):
    """A requested Last-Event-ID has fallen outside the retained history."""


@dataclass(frozen=True, slots=True)
class ResearchRunEvent:
    """One ordered event. ``sequence=None`` is an unnumbered stream control."""

    sequence: int | None
    event: str
    data: dict[str, object]


@dataclass(slots=True)
class _RunEvents:
    task_id: UUID
    run_id: UUID
    events: deque[ResearchRunEvent] = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    next_sequence: int = 1
    terminal: bool = False


class ResearchRunEventBus:
    """Keep bounded per-run SSE history within one running application instance."""

    def __init__(
        self,
        *,
        history_limit: int = 256,
        completed_run_limit: int = 128,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        if history_limit < 1 or completed_run_limit < 1 or heartbeat_seconds <= 0:
            raise ValueError("Run event retention and heartbeat settings must be positive.")
        self._history_limit = history_limit
        self._completed_run_limit = completed_run_limit
        self._heartbeat_seconds = heartbeat_seconds
        self._runs: dict[UUID, _RunEvents] = {}
        self._completed_runs: deque[UUID] = deque()

    def register_run(self, task_id: UUID, run_id: UUID) -> None:
        """Reserve history before the executor starts, so early events are retained."""
        existing = self._runs.get(run_id)
        if existing is not None:
            if existing.task_id != task_id:
                raise ValueError("Run events belong to a different task.")
            return
        run = _RunEvents(task_id=task_id, run_id=run_id)
        self._runs[run_id] = run
        self._append(run, "run.created", {"status": ResearchRunStatus.PENDING.value})

    async def publish_resume(
        self, task_id: UUID, run_id: UUID, *, previous_stage: ResearchRunStatus, next_stage: ResearchRunStatus
    ) -> None:
        """Begin a new process-local event segment for an explicit recovery.

        This is not a durable replay across service restarts. A previously
        interrupted local stream is reopened, preserving monotonically
        increasing event IDs for clients that reconnect on this process.
        """
        run = self._runs.get(run_id)
        if run is None:
            run = _RunEvents(task_id=task_id, run_id=run_id)
            self._runs[run_id] = run
        elif run.task_id != task_id:
            raise ValueError("Run events belong to a different task.")
        async with run.condition:
            if run.terminal:
                if not run.events or run.events[-1].event != "run.interrupted":
                    raise RuntimeError("Cannot reopen a completed or failed SSE stream.")
                run.terminal = False
                try:
                    self._completed_runs.remove(run_id)
                except ValueError:
                    pass
            self._append(
                run,
                "run.resumed",
                {"previous_stage": previous_stage.value, "next_stage": next_stage.value},
            )
            run.condition.notify_all()

    def assert_available(self, task_id: UUID, run_id: UUID, last_event_id: int | None) -> None:
        """Validate a resume cursor before HTTP response headers are committed."""
        run = self._get(task_id, run_id)
        if last_event_id is None:
            return
        oldest = run.events[0].sequence
        assert oldest is not None
        if last_event_id < oldest - 1:
            raise RunEventHistoryExpired("Requested run event history has expired.")
        if last_event_id >= run.next_sequence:
            raise ValueError("Last-Event-ID is newer than the latest run event.")

    async def publish_stage(self, task_id: UUID, run_id: UUID, status: ResearchRunStatus) -> None:
        completed = status == ResearchRunStatus.COMPLETED
        await self._publish(
            task_id,
            run_id,
            "run.completed" if completed else "run.stage",
            {"status": status.value},
            terminal=completed,
        )

    async def publish_failure(self, task_id: UUID, run_id: UUID, error: str) -> None:
        await self._publish(
            task_id,
            run_id,
            "run.failed",
            {"status": ResearchRunStatus.FAILED.value, "error": error},
            terminal=True,
        )

    async def publish_interruption(self, task_id: UUID, run_id: UUID) -> None:
        """The in-process executor stopped; this does not persist a run transition."""
        await self._publish(task_id, run_id, "run.interrupted", {"reason": "executor_stopped"}, terminal=True)

    async def publish_evidence_progress(self, progress: EvidenceExtractionProgress) -> None:
        payload = asdict(progress)
        payload.update(
            remaining_chunk_count=progress.remaining_chunk_count,
            completion_ratio=progress.completion_ratio,
        )
        await self._publish(progress.task_id, progress.run_id, "evidence.progress", payload)

    async def _publish(
        self,
        task_id: UUID,
        run_id: UUID,
        event: str,
        data: dict[str, object],
        *,
        terminal: bool = False,
    ) -> None:
        run = self._get(task_id, run_id)
        async with run.condition:
            if run.terminal:
                return
            self._append(run, event, data)
            if terminal:
                run.terminal = True
            run.condition.notify_all()
        if terminal:
            self._completed_runs.append(run_id)
            while len(self._completed_runs) > self._completed_run_limit:
                old_run_id = self._completed_runs.popleft()
                self._runs.pop(old_run_id, None)

    def _get(self, task_id: UUID, run_id: UUID) -> _RunEvents:
        run = self._runs.get(run_id)
        if run is None or run.task_id != task_id:
            raise LookupError("Run event history is not available on this worker.")
        return run

    def _append(self, run: _RunEvents, event: str, data: dict[str, object]) -> None:
        payload = {**data, "task_id": str(run.task_id), "run_id": str(run.run_id)}
        run.events.append(
            ResearchRunEvent(
                sequence=run.next_sequence,
                event=event,
                data=payload,
            )
        )
        run.next_sequence += 1
        while len(run.events) > self._history_limit:
            run.events.popleft()

    async def stream(
        self, task_id: UUID, run_id: UUID, *, last_event_id: int | None = None
    ) -> AsyncIterator[ResearchRunEvent | None]:
        """Replay retained history then follow new events; ``None`` is a heartbeat.

        A lagging reader receives a non-replayable ``stream.reset`` control and
        closes. It must GET the run snapshot and reconnect without a cursor.
        """
        run = self._get(task_id, run_id)
        cursor = last_event_id
        if cursor is None:
            first_sequence = run.events[0].sequence
            assert first_sequence is not None
            cursor = first_sequence - 1

        while True:
            timed_out = False
            reset: ResearchRunEvent | None = None
            async with run.condition:
                first_sequence = run.events[0].sequence
                assert first_sequence is not None
                if cursor < first_sequence - 1:
                    reset = ResearchRunEvent(
                        sequence=None,
                        event="stream.reset",
                        data={
                            "task_id": str(task_id),
                            "run_id": str(run_id),
                            "reason": "history_expired",
                        },
                    )
                    pending: list[ResearchRunEvent] = []
                else:
                    pending = [event for event in run.events if event.sequence is not None and event.sequence > cursor]

                terminal = run.terminal
                if not pending and not terminal and reset is None:
                    try:
                        await asyncio.wait_for(run.condition.wait(), timeout=self._heartbeat_seconds)
                    except TimeoutError:
                        timed_out = True

            if reset is not None:
                yield reset
                return
            if pending:
                for event in pending:
                    assert event.sequence is not None
                    cursor = event.sequence
                    yield event
            elif terminal:
                return
            elif timed_out:
                yield None
