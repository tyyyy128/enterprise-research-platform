from __future__ import annotations

import asyncio

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


async def create_supported_claim(
    client: AsyncClient,
) -> tuple[str, str]:
    task_response = await client.post(
        "/api/enterprise-research/tasks",
        json={"query": "Research LangGraph durable execution"},
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
            "content": "LangGraph supports durable execution.",
            "locator": "section: durable-execution",
            "relevance_score": 0.95,
        },
    )

    assert evidence_response.status_code == 201
    evidence_id = evidence_response.json()["evidence_id"]

    claim_response = await client.post(
        f"/api/enterprise-research/tasks/{task_id}/claims",
        json={
            "text": "LangGraph supports durable execution.",
        },
    )

    assert claim_response.status_code == 201
    claim_id = claim_response.json()["claim_id"]

    support_response = await client.post(
        (f"/api/enterprise-research/tasks/{task_id}/claims/{claim_id}/support"),
        json={
            "evidence_ids": [evidence_id],
            "confidence": 0.95,
        },
    )

    assert support_response.status_code == 200

    return task_id, claim_id


def test_build_report_from_supported_claim() -> None:
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
                task_id, claim_id = await create_supported_claim(client)

                response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/report",
                    json={
                        "title": "LangGraph Durable Execution",
                        "summary": "Research summary.",
                        "claim_ids": [claim_id],
                    },
                )

                assert response.status_code == 201

                report = response.json()

                assert report["task_id"] == task_id
                assert report["title"] == ("LangGraph Durable Execution")
                assert report["claim_ids"] == [claim_id]

                loaded = await client.get(f"/api/enterprise-research/tasks/{task_id}")

                assert loaded.status_code == 200

                task = loaded.json()

                assert task["status"] == "completed"
                assert task["report_id"] == report["report_id"]
        finally:
            await service.stop()

    asyncio.run(run())


def test_report_rejects_unsupported_claim() -> None:
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

                task_id = task_response.json()["task_id"]

                claim_response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/claims",
                    json={
                        "text": "Unverified statement.",
                    },
                )

                claim_id = claim_response.json()["claim_id"]

                response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/report",
                    json={
                        "title": "Invalid Report",
                        "claim_ids": [claim_id],
                    },
                )

                assert response.status_code == 400
                assert "supported claims" in (response.json()["detail"].lower())
        finally:
            await service.stop()

    asyncio.run(run())
