"""Application service for enterprise research."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from enterprise_research.application.errors import (
    ResearchTaskNotFoundError,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.domain import (
    Claim,
    Evidence,
    ResearchAggregate,
    ResearchReport,
    Source,
)


class ResearchApplicationService:
    """Coordinates research use cases and persistence."""

    def __init__(
        self,
        repository: ResearchRepository,
    ) -> None:
        self._repository = repository

    async def create_task(
        self,
        query: str,
    ) -> ResearchAggregate:
        aggregate = ResearchAggregate.create(query)

        await self._repository.save(aggregate)

        return aggregate

    async def get_task(
        self,
        task_id: UUID,
    ) -> ResearchAggregate:
        aggregate = await self._repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        return aggregate

    async def add_source(
        self,
        task_id: UUID,
        source: Source,
    ) -> Source:
        aggregate = await self.get_task(task_id)

        aggregate.add_source(source)

        await self._repository.save(aggregate)

        return source

    async def add_evidence(
        self,
        task_id: UUID,
        evidence: Evidence,
    ) -> Evidence:
        aggregate = await self.get_task(task_id)

        aggregate.add_evidence(evidence)

        await self._repository.save(aggregate)

        return evidence

    async def add_claim(
        self,
        task_id: UUID,
        claim: Claim,
    ) -> Claim:
        aggregate = await self.get_task(task_id)

        aggregate.add_claim(claim)

        await self._repository.save(aggregate)

        return claim

    async def support_claim(
        self,
        task_id: UUID,
        claim_id: UUID,
        evidence_ids: Sequence[UUID],
        *,
        confidence: float,
    ) -> Claim:
        aggregate = await self.get_task(task_id)

        claim = aggregate.support_claim(
            claim_id,
            evidence_ids,
            confidence=confidence,
        )

        await self._repository.save(aggregate)

        return claim

    async def trace_claim(
        self,
        task_id: UUID,
        claim_id: UUID,
    ) -> list[tuple[Evidence, Source]]:
        aggregate = await self.get_task(task_id)

        return aggregate.trace_claim(claim_id)

    async def build_report(
        self,
        task_id: UUID,
        *,
        title: str,
        summary: str = "",
        claim_ids: Sequence[UUID] | None = None,
    ) -> ResearchReport:
        aggregate = await self.get_task(task_id)

        report = aggregate.build_report(
            title=title,
            summary=summary,
            claim_ids=claim_ids,
        )

        await self._repository.save(aggregate)

        return report
