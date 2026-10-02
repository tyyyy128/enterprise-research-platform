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


async def create_task_source_evidence(
    client: AsyncClient,
) -> tuple[str, str, str]:
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
            "content": "LangGraph supports durable execution.",
            "locator": "section: durable-execution",
            "relevance_score": 0.95,
        },
    )
    assert evidence_response.status_code == 201
    evidence_id = evidence_response.json()["evidence_id"]

    return task_id, source_id, evidence_id


def test_claim_support_and_lineage_flow() -> None:
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
                task_id, source_id, evidence_id = await create_task_source_evidence(client)

                claim_response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/claims",
                    json={
                        "text": "LangGraph supports durable execution.",
                    },
                )

                assert claim_response.status_code == 201

                claim = claim_response.json()

                assert claim["status"] == "unsupported"
                assert claim["evidence_ids"] == []
                assert claim["confidence"] == 0.0

                claim_id = claim["claim_id"]

                support_response = await client.post(
                    (f"/api/enterprise-research/tasks/{task_id}/claims/{claim_id}/support"),
                    json={
                        "evidence_ids": [evidence_id],
                        "confidence": 0.95,
                    },
                )

                assert support_response.status_code == 200

                supported = support_response.json()

                assert supported["status"] == "supported"
                assert supported["evidence_ids"] == [evidence_id]
                assert supported["confidence"] == 0.95

                lineage_response = await client.get(
                    f"/api/enterprise-research/tasks/{task_id}/claims/{claim_id}/lineage"
                )

                assert lineage_response.status_code == 200

                lineage = lineage_response.json()

                assert lineage["claim_id"] == claim_id
                assert len(lineage["items"]) == 1

                item = lineage["items"][0]

                assert item["evidence"]["evidence_id"] == evidence_id
                assert item["source"]["source_id"] == source_id

                loaded = await client.get(f"/api/enterprise-research/tasks/{task_id}")

                assert loaded.status_code == 200
                assert loaded.json()["claim_count"] == 1
        finally:
            await service.stop()

    asyncio.run(run())


def test_claim_rejects_unknown_evidence() -> None:
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

                response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/claims",
                    json={
                        "text": "Untraceable claim.",
                        "evidence_ids": [str(uuid4())],
                    },
                )

                assert response.status_code == 404
                assert "unknown evidence" in (response.json()["detail"].lower())
        finally:
            await service.stop()

    asyncio.run(run())


def test_support_rejects_unknown_claim() -> None:
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
                task_id, _, evidence_id = await create_task_source_evidence(client)

                response = await client.post(
                    (f"/api/enterprise-research/tasks/{task_id}/claims/{uuid4()}/support"),
                    json={
                        "evidence_ids": [evidence_id],
                        "confidence": 0.9,
                    },
                )

                assert response.status_code == 404
                assert "unknown claim" in (response.json()["detail"].lower())
        finally:
            await service.stop()

    asyncio.run(run())


def test_support_rejects_unknown_evidence() -> None:
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
                task_id, _, _ = await create_task_source_evidence(client)

                claim_response = await client.post(
                    f"/api/enterprise-research/tasks/{task_id}/claims",
                    json={"text": "Candidate claim."},
                )

                claim_id = claim_response.json()["claim_id"]

                response = await client.post(
                    (f"/api/enterprise-research/tasks/{task_id}/claims/{claim_id}/support"),
                    json={
                        "evidence_ids": [str(uuid4())],
                        "confidence": 0.9,
                    },
                )

                assert response.status_code == 404
                assert "unknown evidence" in (response.json()["detail"].lower())
        finally:
            await service.stop()

    asyncio.run(run())
