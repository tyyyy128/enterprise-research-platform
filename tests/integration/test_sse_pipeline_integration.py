"""End-to-end SSE checks with the actual router, executor, pipeline and event bus.

The planning and evidence providers are deterministic fakes: this suite tests
streaming orchestration, not external LLM or search services.  Live-connection
tests use raw ASGI rather than httpx.ASGITransport, which buffers streaming
responses until the app finishes sending them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchPipelineStageRunner,
    ResearchRunApplicationService,
    ResearchRunExecutor,
)
from enterprise_research.application.evidence_progress import EvidenceExtractionProgress
from enterprise_research.application.run_events import ResearchRunEventBus
from enterprise_research.domain import ResearchRunStatus
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)
from enterprise_research.plugin import build_router

PREFIX = "/api/enterprise-research"


class NoNetworkPlanner:
    async def create_plan(self, task_id: UUID, run_id: UUID, *, lease=None) -> None:
        del task_id, run_id
        await asyncio.sleep(0)


class ControlledEvidence:
    """Expose the VERIFYING phase while it is running; never call a provider."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail
        self.calls: list[tuple[UUID, UUID]] = []

    async def extract_run(self, task_id: UUID, run_id: UUID, *, progress_callback=None, lease=None) -> None:
        assert progress_callback is not None
        self.calls.append((task_id, run_id))
        await progress_callback(progress(task_id, run_id, 0))
        self.entered.set()
        await self.release.wait()
        await progress_callback(progress(task_id, run_id, 1))
        if self.fail:
            raise RuntimeError("controlled evidence provider failure")
        await asyncio.sleep(0)
        await progress_callback(progress(task_id, run_id, 2))


def progress(task_id: UUID, run_id: UUID, completed: int) -> EvidenceExtractionProgress:
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


class ApiDependencies:
    def __init__(
        self,
        application: ResearchApplicationService,
        run_application: ResearchRunApplicationService,
        executor: ResearchRunExecutor,
        bus: ResearchRunEventBus,
    ) -> None:
        self.application = application
        self.run_application = run_application
        self.executor = executor
        self.bus = bus

    async def require_deps(self) -> None:
        return None

    async def require_application(self) -> ResearchApplicationService:
        return self.application

    async def require_run_application(self) -> ResearchRunApplicationService:
        return self.run_application

    async def require_run_executor(self) -> ResearchRunExecutor:
        return self.executor

    async def require_run_event_bus(self) -> ResearchRunEventBus:
        return self.bus


@dataclass
class Runtime:
    app: FastAPI
    executor: ResearchRunExecutor
    run_application: ResearchRunApplicationService
    bus: ResearchRunEventBus
    evidence: ControlledEvidence


def build_runtime(*, fail: bool = False, history_limit: int = 64) -> Runtime:
    repository = InMemoryResearchRepository()
    application = ResearchApplicationService(repository)
    run_application = ResearchRunApplicationService(repository, InMemoryResearchRunRepository())
    bus = ResearchRunEventBus(history_limit=history_limit, heartbeat_seconds=0.05)
    evidence = ControlledEvidence(fail=fail)
    pipeline = ResearchPipelineStageRunner(
        NoNetworkPlanner(),  # type: ignore[arg-type]
        evidence_extraction_service=evidence,  # type: ignore[arg-type]
        evidence_progress_callback=bus.publish_evidence_progress,
    )
    executor = ResearchRunExecutor(run_application, pipeline, event_bus=bus)
    app = FastAPI()
    app.include_router(
        build_router(ApiDependencies(application, run_application, executor, bus))  # type: ignore[arg-type]
    )
    return Runtime(app, executor, run_application, bus, evidence)


async def create_run(client: AsyncClient, query: str = "Integration research") -> tuple[str, str]:
    task_response = await client.post(f"{PREFIX}/tasks", json={"query": query})
    assert task_response.status_code == 201, task_response.text
    task_id = task_response.json()["task_id"]
    run_response = await client.post(f"{PREFIX}/tasks/{task_id}/runs")
    assert run_response.status_code == 201, run_response.text
    return task_id, run_response.json()["run_id"]


def event_path(task_id: str, run_id: str) -> str:
    return f"{PREFIX}/tasks/{task_id}/runs/{run_id}/events"


async def await_idle(executor: ResearchRunExecutor) -> None:
    async def wait() -> None:
        while executor.active_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=2)


def parse_frames(data: str) -> list[dict[str, Any]]:
    events = []
    for frame in data.split("\n\n"):
        fields = {}
        for line in frame.splitlines():
            if line.startswith(":") or not line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value.lstrip(" ")
        if "event" in fields:
            events.append(
                {
                    "id": int(fields["id"]) if "id" in fields else None,
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return events


class LiveASGIStream:
    """Drive an actual GET request without collecting the entire response body."""

    def __init__(self, app: FastAPI, path: str) -> None:
        self.app = app
        self.path = path
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LiveASGIStream:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode("ascii"),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 45678),
            "server": ("test", 80),
        }
        await self.incoming.put({"type": "http.request", "body": b"", "more_body": False})
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        if self.task is None:
            return
        if not self.task.done():
            await self.disconnect()
        assert self.task.done()
        await self.task

    async def next_message(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.outgoing.get(), timeout=2)

    async def until_event(self, target: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        while True:
            message = await self.next_message()
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                continue
            assert message["type"] == "http.response.body"
            if message.get("body"):
                found.extend(parse_frames(message["body"].decode("utf-8")))
            if any(event["event"] == target for event in found):
                return found
            assert message.get("more_body"), f"Stream ended before {target!r}"

    async def disconnect(self) -> None:
        if self.task is None or self.task.done():
            return
        await self.incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, timeout=2)


def test_e2e_completed_run_emits_ordered_progress_and_replays_without_duplicates() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            runtime.evidence.release.set()
            await await_idle(runtime.executor)
            response = await client.get(event_path(task_id, run_id))
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = parse_frames(response.text)
            assert [event["event"] for event in events] == [
                "run.created",
                "run.stage",
                "run.stage",
                "run.stage",
                "run.stage",
                "evidence.progress",
                "evidence.progress",
                "evidence.progress",
                "run.stage",
                "run.completed",
            ]
            assert [event["id"] for event in events] == list(range(1, len(events) + 1))
            counts = [e["data"]["completed_chunk_count"] for e in events if e["event"] == "evidence.progress"]
            assert counts == [0, 1, 2]
            assert all(event["data"]["task_id"] == task_id for event in events)
            assert all(event["data"]["run_id"] == run_id for event in events)
            assert events[-1]["data"]["status"] == "completed"
            assert runtime.evidence.calls == [(UUID(task_id), UUID(run_id))]
            saved = await runtime.run_application.get_run(UUID(task_id), UUID(run_id))
            assert saved.status == ResearchRunStatus.COMPLETED

            # Reconnect after the VERIFYING stage: no duplicate delivery.
            cursor = events[4]["id"]
            replay = await client.get(event_path(task_id, run_id), headers={"Last-Event-ID": str(cursor)})
            assert replay.status_code == 200
            assert parse_frames(replay.text) == events[5:]

    asyncio.run(exercise())


def test_live_http_stream_receives_progress_before_run_finishes() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            async with LiveASGIStream(runtime.app, event_path(task_id, run_id)) as stream:
                initial = await stream.until_event("evidence.progress")
                assert initial[-1]["data"]["completed_chunk_count"] == 0
                assert runtime.executor.active_count == 1
                runtime.evidence.release.set()
                finished = await stream.until_event("run.completed")
                counts = [e["data"]["completed_chunk_count"] for e in finished if e["event"] == "evidence.progress"]
                assert counts == [1, 2]
            await await_idle(runtime.executor)

    asyncio.run(exercise())


def test_disconnecting_live_sse_does_not_cancel_the_background_run() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            async with LiveASGIStream(runtime.app, event_path(task_id, run_id)) as stream:
                await stream.until_event("evidence.progress")
                await stream.disconnect()
            assert runtime.executor.active_count == 1
            still_running = await runtime.run_application.get_run(UUID(task_id), UUID(run_id))
            assert still_running.status == ResearchRunStatus.VERIFYING

            runtime.evidence.release.set()
            await await_idle(runtime.executor)
            final = await client.get(event_path(task_id, run_id))
            assert parse_frames(final.text)[-1]["event"] == "run.completed"

    asyncio.run(exercise())


def test_failed_run_has_terminal_error_and_expired_resume_cursor_is_409() -> None:
    async def exercise() -> None:
        runtime = build_runtime(fail=True, history_limit=3)
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            runtime.evidence.release.set()
            await await_idle(runtime.executor)
            response = await client.get(event_path(task_id, run_id))
            assert response.status_code == 200
            tail = parse_frames(response.text)
            assert [e["event"] for e in tail] == ["evidence.progress", "evidence.progress", "run.failed"]
            assert tail[-1]["data"]["error"] == "controlled evidence provider failure"
            assert tail[-1]["data"]["status"] == "failed"
            persisted = await runtime.run_application.get_run(UUID(task_id), UUID(run_id))
            assert persisted.status == ResearchRunStatus.FAILED
            assert persisted.error == "controlled evidence provider failure"
            expired = await client.get(event_path(task_id, run_id), headers={"Last-Event-ID": "1"})
            assert expired.status_code == 409
            valid = await client.get(event_path(task_id, run_id), headers={"Last-Event-ID": str(tail[0]["id"] - 1)})
            assert parse_frames(valid.text) == tail

    asyncio.run(exercise())


def test_executor_shutdown_ends_live_stream_with_interruption_not_failure() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            async with LiveASGIStream(runtime.app, event_path(task_id, run_id)) as stream:
                await stream.until_event("evidence.progress")
                await runtime.executor.shutdown()
                ending = await stream.until_event("run.interrupted")
                assert ending[-1]["data"]["reason"] == "executor_stopped"
            saved = await runtime.run_application.get_run(UUID(task_id), UUID(run_id))
            assert saved.status == ResearchRunStatus.VERIFYING
            assert saved.error is None
            assert runtime.executor.active_count == 0

    asyncio.run(exercise())


def test_simultaneous_runs_never_cross_publish_progress_events() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_a, run_a = await create_run(client, "Run A")
            task_b, run_b = await create_run(client, "Run B")
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            runtime.evidence.release.set()
            await await_idle(runtime.executor)
            expected_calls = [(UUID(task_a), UUID(run_a)), (UUID(task_b), UUID(run_b))]
            assert sorted(runtime.evidence.calls) == sorted(expected_calls)
            for task_id, run_id in ((task_a, run_a), (task_b, run_b)):
                response = await client.get(event_path(task_id, run_id))
                events = parse_frames(response.text)
                assert events[-1]["event"] == "run.completed"
                assert all(event["data"]["task_id"] == task_id for event in events)
                assert all(event["data"]["run_id"] == run_id for event in events)
                assert [e["id"] for e in events] == list(range(1, len(events) + 1))

    asyncio.run(exercise())


def test_heartbeats_are_comments_without_event_ids_and_do_not_stop_runs() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            async with LiveASGIStream(runtime.app, event_path(task_id, run_id)) as stream:
                await stream.until_event("evidence.progress")
                while True:
                    message = await stream.next_message()
                    if message["type"] == "http.response.body" and message.get("body"):
                        assert message["body"] == b": keep-alive\n\n"
                        break
                assert runtime.executor.active_count == 1
            runtime.evidence.release.set()
            await await_idle(runtime.executor)
            frames = parse_frames((await client.get(event_path(task_id, run_id))).text)
            assert [e["id"] for e in frames] == list(range(1, len(frames) + 1))

    asyncio.run(exercise())


def test_two_live_subscribers_are_independent_when_one_disconnects() -> None:
    async def exercise() -> None:
        runtime = build_runtime()
        async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://test") as client:
            task_id, run_id = await create_run(client)
            await asyncio.wait_for(runtime.evidence.entered.wait(), timeout=2)
            async with (
                LiveASGIStream(runtime.app, event_path(task_id, run_id)) as first,
                LiveASGIStream(runtime.app, event_path(task_id, run_id)) as second,
            ):
                await first.until_event("evidence.progress")
                await second.until_event("evidence.progress")
                await first.disconnect()
                assert runtime.executor.active_count == 1
                runtime.evidence.release.set()
                tail = await second.until_event("run.completed")
                assert tail[-1]["event"] == "run.completed"
            await await_idle(runtime.executor)

    asyncio.run(exercise())
