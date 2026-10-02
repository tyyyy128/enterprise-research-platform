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


def test_add_evidence_to_known_source() -> None:
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
                        "url": "https://example.com/langgraph",
                    },
                )

                assert source_response.status_code == 201
                source_id = source_response.json()["source_id"]

                evidence_response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/evidence",
                    json={
                        "source_id": source_id,
                        "content": ("LangGraph supports durable execution."),
                        "locator": "section: durable-execution",
                        "relevance_score": 0.95,
                    },
                )

                assert evidence_response.status_code == 201

                evidence = evidence_response.json()

                assert evidence["source_id"] == source_id
                assert evidence["content"] == ("LangGraph supports durable execution.")
                assert evidence["locator"] == ("section: durable-execution")
                assert evidence["relevance_score"] == 0.95

                loaded = await client.get(f"/api/enterprise-research/tasks/{task_id}")

                assert loaded.status_code == 200
                assert loaded.json()["source_count"] == 1
                assert loaded.json()["evidence_count"] == 1
        finally:
            await service.stop()

    asyncio.run(run())


def test_evidence_rejects_unknown_source() -> None:
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

                response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/evidence",
                    json={
                        "source_id": str(uuid4()),
                        "content": "Untraceable evidence.",
                        "relevance_score": 0.5,
                    },
                )

                assert response.status_code == 404
                assert "unknown source" in response.json()["detail"].lower()
        finally:
            await service.stop()

    asyncio.run(run())
