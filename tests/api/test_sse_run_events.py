"""SSE HTTP surface tests for process-local research progress."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps, HostPolicySnapshot
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise_research.plugin import EnterpriseResearchService, build_router


@pytest.fixture(autouse=True)
def _disable_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
    for key in (
        "ENTERPRISE_RESEARCH_PLANNER_BACKEND",
        "ENTERPRISE_RESEARCH_SEARCH_BACKEND",
        "ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND",
        "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
    ):
        monkeypatch.setenv(key, "disabled")


def runtime_deps() -> ExtensionRuntimeDeps:
    return ExtensionRuntimeDeps(
        app_store=ExtensionData("app"),
        policy=HostPolicySnapshot(max_subagents_per_run=3),
        session_factory=object(),
    )


def parse_sse(data: str) -> list[dict[str, object]]:
    events = []
    for frame in data.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":"))
        events.append(
            {
                "id": int(fields["id"]),
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return events


def test_api_streams_run_lifecycle_and_supports_last_event_id() -> None:
    async def run() -> None:
        service = EnterpriseResearchService()
        app = FastAPI()
        app.include_router(build_router(service))
        await service.start(runtime_deps())
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                task_response = await client.post("/api/enterprise-research/tasks", json={"query": "stream research"})
                assert task_response.status_code == 201
                task_id = task_response.json()["task_id"]
                run_response = await client.post(f"/api/enterprise-research/tasks/{task_id}/runs")
                assert run_response.status_code == 201
                run_id = run_response.json()["run_id"]
                path = f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}/events"

                # A terminal stream finishes, so ASGITransport can consume it in full.
                for _ in range(100):
                    status_response = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}")
                    if status_response.json()["status"] == "completed":
                        break
                    await asyncio.sleep(0)
                else:
                    raise AssertionError("Run did not finish")

                stream = await client.get(path)
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith("text/event-stream")
                assert stream.headers["cache-control"] == "no-cache, no-transform"
                events = parse_sse(stream.text)
                assert events[0]["event"] == "run.created"
                assert events[-1]["event"] == "run.completed"
                assert [event["id"] for event in events] == list(range(1, len(events) + 1))
                assert events[-1]["data"]["run_id"] == run_id

                replay = await client.get(path, headers={"Last-Event-ID": "2"})
                assert replay.status_code == 200
                assert parse_sse(replay.text) == events[2:]
                invalid = await client.get(path, headers={"Last-Event-ID": "1.5"})
                assert invalid.status_code == 400
                future = await client.get(path, headers={"Last-Event-ID": "99999"})
                assert future.status_code == 400
                wrong_task = await client.get(f"/api/enterprise-research/tasks/{uuid4()}/runs/{run_id}/events")
                assert wrong_task.status_code == 404
                missing = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{uuid4()}/events")
                assert missing.status_code == 404
        finally:
            await service.stop()

    asyncio.run(run())
