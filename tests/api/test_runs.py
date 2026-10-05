from __future__ import annotations

import asyncio
from uuid import uuid4

from deerflow_extension_api import (
    ExtensionData,
    ExtensionRuntimeDeps,
    HostPolicySnapshot,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise_research.plugin import (
    EnterpriseResearchService,
    build_router,
)


def create_runtime_deps() -> ExtensionRuntimeDeps:
    return ExtensionRuntimeDeps(
        app_store=ExtensionData("app"),
        policy=HostPolicySnapshot(max_subagents_per_run=3),
        session_factory=object(),
    )


def test_create_and_get_research_run() -> None:
    async def run() -> None:
        service = EnterpriseResearchService()

        app = FastAPI()
        app.include_router(build_router(service))

        await service.start(create_runtime_deps())

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                task_response = await client.post(
                    "/api/enterprise-research/tasks",
                    json={"query": "Research durable execution"},
                )

                assert task_response.status_code == 201
                task_id = task_response.json()["task_id"]

                create_response = await client.post(f"/api/enterprise-research/tasks/{task_id}/runs")

                assert create_response.status_code == 201

                created = create_response.json()

                assert created["task_id"] == task_id
                assert created["status"] == "pending"
                assert created["started_at"] is None
                assert created["completed_at"] is None
                assert created["error"] is None

                run_id = created["run_id"]

                get_response = await client.get(f"/api/enterprise-research/tasks/{task_id}/runs/{run_id}")

                assert get_response.status_code == 200
                assert get_response.json() == created
        finally:
            await service.stop()

    asyncio.run(run())


def test_create_run_for_unknown_task_returns_404() -> None:
    async def run() -> None:
        service = EnterpriseResearchService()

        app = FastAPI()
        app.include_router(build_router(service))

        await service.start(create_runtime_deps())

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(f"/api/enterprise-research/tasks/{uuid4()}/runs")

                assert response.status_code == 404
                assert "Research task not found" in (response.json()["detail"])
        finally:
            await service.stop()

    asyncio.run(run())


def test_run_cannot_be_read_through_another_task() -> None:
    async def run() -> None:
        service = EnterpriseResearchService()

        app = FastAPI()
        app.include_router(build_router(service))

        await service.start(create_runtime_deps())

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                first = await client.post(
                    "/api/enterprise-research/tasks",
                    json={"query": "First task"},
                )
                second = await client.post(
                    "/api/enterprise-research/tasks",
                    json={"query": "Second task"},
                )

                first_task_id = first.json()["task_id"]
                second_task_id = second.json()["task_id"]

                created = await client.post(f"/api/enterprise-research/tasks/{first_task_id}/runs")

                run_id = created.json()["run_id"]

                response = await client.get(f"/api/enterprise-research/tasks/{second_task_id}/runs/{run_id}")

                assert response.status_code == 404
                assert "Research run not found" in (response.json()["detail"])
        finally:
            await service.stop()

    asyncio.run(run())
