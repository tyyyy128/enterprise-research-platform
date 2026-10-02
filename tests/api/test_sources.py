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


def test_add_source_to_research_task() -> None:
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
                    json={"query": "Research LangGraph"},
                )

                assert task_response.status_code == 201

                task_id = task_response.json()["task_id"]

                source_response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/sources",
                    json={
                        "source_type": "web",
                        "title": "LangGraph documentation",
                        "url": "https://docs.langchain.com/oss/python/langgraph",
                        "metadata": {
                            "provider": "manual-test",
                        },
                    },
                )

                assert source_response.status_code == 201

                source = source_response.json()

                assert source["source_type"] == "web"
                assert source["title"] == "LangGraph documentation"
                assert source["metadata"]["provider"] == "manual-test"

                loaded = await client.get(f"/api/enterprise-research/tasks/{task_id}")

                assert loaded.status_code == 200
                assert loaded.json()["source_count"] == 1
        finally:
            await service.stop()

    asyncio.run(run())


def test_add_source_to_unknown_task_returns_404() -> None:
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
                response = await client.post(
                    f"/api/enterprise-research/tasks/{uuid4()}/sources",
                    json={
                        "source_type": "web",
                        "title": "Unknown task source",
                        "url": "https://example.com",
                    },
                )

                assert response.status_code == 404
                assert "Research task not found" in response.json()["detail"]
        finally:
            await service.stop()

    asyncio.run(run())
