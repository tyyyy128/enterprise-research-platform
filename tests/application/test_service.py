from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchTaskNotFoundError,
)
from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    Source,
    SourceType,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
)


def test_create_and_get_task() -> None:
    async def run() -> None:
        service = ResearchApplicationService(InMemoryResearchRepository())

        created = await service.create_task("Research LangGraph")

        loaded = await service.get_task(created.task.task_id)

        assert loaded.task.task_id == created.task.task_id
        assert loaded.task.query == "Research LangGraph"

    asyncio.run(run())


def test_get_unknown_task_raises() -> None:
    async def run() -> None:
        service = ResearchApplicationService(InMemoryResearchRepository())

        with pytest.raises(ResearchTaskNotFoundError):
            await service.get_task(uuid4())

    asyncio.run(run())


def test_full_research_flow() -> None:
    async def run() -> None:
        service = ResearchApplicationService(InMemoryResearchRepository())

        aggregate = await service.create_task("How does LangGraph durable execution work?")

        source = await service.add_source(
            aggregate.task.task_id,
            Source(
                source_type=SourceType.WEB,
                title="LangGraph documentation",
                url="https://example.com/langgraph",
            ),
        )

        evidence = await service.add_evidence(
            aggregate.task.task_id,
            Evidence(
                source_id=source.source_id,
                content=("LangGraph supports durable execution."),
                locator="section: durable-execution",
            ),
        )

        claim = await service.add_claim(
            aggregate.task.task_id,
            Claim(
                text=("LangGraph supports durable execution."),
            ),
        )

        supported = await service.support_claim(
            aggregate.task.task_id,
            claim.claim_id,
            [evidence.evidence_id],
            confidence=0.95,
        )

        lineage = await service.trace_claim(
            aggregate.task.task_id,
            supported.claim_id,
        )

        report = await service.build_report(
            aggregate.task.task_id,
            title="LangGraph Durable Execution",
        )

        assert supported.status == ClaimStatus.SUPPORTED
        assert lineage == [(evidence, source)]
        assert report.claim_ids == [supported.claim_id]

    asyncio.run(run())
