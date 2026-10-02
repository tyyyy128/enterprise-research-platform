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


def test_create_and_get_research_task() -> None:
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
                create_response = await client.post(
                    "/api/enterprise-research/tasks",
                    json={"query": ("How does LangGraph durable execution work?")},
                )

                assert create_response.status_code == 201

                created = create_response.json()

                assert created["query"] == ("How does LangGraph durable execution work?")
                assert created["status"] == "created"
                assert created["source_count"] == 0
                assert created["evidence_count"] == 0
                assert created["claim_count"] == 0

                task_id = created["task_id"]

                get_response = await client.get(f"/api/enterprise-research/tasks/{task_id}")

                assert get_response.status_code == 200
                assert get_response.json()["task_id"] == task_id
                assert get_response.json()["query"] == created["query"]
        finally:
            await service.stop()

    asyncio.run(run())


def test_get_unknown_research_task_returns_404() -> None:
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
                response = await client.get(f"/api/enterprise-research/tasks/{uuid4()}")

                assert response.status_code == 404
                assert "Research task not found" in response.json()["detail"]
        finally:
            await service.stop()

    asyncio.run(run())
