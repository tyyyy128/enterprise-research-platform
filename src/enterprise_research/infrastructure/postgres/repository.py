"""PostgreSQL repository for research aggregates."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    ResearchAggregate,
    ResearchReport,
    ResearchTask,
    ResearchTaskStatus,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ReportClaimRow,
    ResearchClaimRow,
    ResearchEvidenceRow,
    ResearchReportRow,
    ResearchSourceRow,
    ResearchTaskRow,
)


class PostgresResearchRepository:
    """Persist complete research aggregates in PostgreSQL."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        aggregate: ResearchAggregate,
    ) -> None:
        """Persist the complete aggregate atomically."""
        task = aggregate.task

        async with self._session_factory() as session:
            async with session.begin():
                # V1 persistence strategy:
                # replace the complete aggregate inside one transaction.
                await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task.task_id))

                session.add(
                    ResearchTaskRow(
                        id=task.task_id,
                        query=task.query,
                        status=task.status.value,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                    )
                )

                await session.flush()

                source_rows = [
                    ResearchSourceRow(
                        id=source.source_id,
                        task_id=task.task_id,
                        source_type=source.source_type.value,
                        title=source.title,
                        url=(str(source.url) if source.url is not None else None),
                        retrieved_at=source.retrieved_at,
                        content_hash=source.content_hash,
                        source_metadata=dict(source.metadata),
                    )
                    for source in aggregate.sources.values()
                ]

                if source_rows:
                    session.add_all(source_rows)
                    await session.flush()

                evidence_rows = [
                    ResearchEvidenceRow(
                        id=evidence.evidence_id,
                        task_id=task.task_id,
                        source_id=evidence.source_id,
                        content=evidence.content,
                        locator=evidence.locator,
                        relevance_score=evidence.relevance_score,
                        created_at=evidence.created_at,
                    )
                    for evidence in aggregate.evidence.values()
                ]

                if evidence_rows:
                    session.add_all(evidence_rows)
                    await session.flush()

                claim_rows = [
                    ResearchClaimRow(
                        id=claim.claim_id,
                        task_id=task.task_id,
                        text=claim.text,
                        status=claim.status.value,
                        confidence=claim.confidence,
                    )
                    for claim in aggregate.claims.values()
                ]

                if claim_rows:
                    session.add_all(claim_rows)
                    await session.flush()

                claim_evidence_rows: list[ClaimEvidenceRow] = []

                for claim in aggregate.claims.values():
                    claim_evidence_rows.extend(
                        ClaimEvidenceRow(
                            claim_id=claim.claim_id,
                            evidence_id=evidence_id,
                            position=position,
                        )
                        for position, evidence_id in enumerate(claim.evidence_ids)
                    )

                if claim_evidence_rows:
                    session.add_all(claim_evidence_rows)
                    await session.flush()

                if aggregate.report is not None:
                    report = aggregate.report

                    session.add(
                        ResearchReportRow(
                            id=report.report_id,
                            task_id=task.task_id,
                            title=report.title,
                            summary=report.summary,
                            generated_at=report.generated_at,
                        )
                    )

                    await session.flush()

                    report_claim_rows = [
                        ReportClaimRow(
                            report_id=report.report_id,
                            claim_id=claim_id,
                            position=position,
                        )
                        for position, claim_id in enumerate(report.claim_ids)
                    ]

                    if report_claim_rows:
                        session.add_all(report_claim_rows)

    async def get(
        self,
        task_id: UUID,
    ) -> ResearchAggregate | None:
        """Load and reconstruct one complete research aggregate."""
        async with self._session_factory() as session:
            task_row = await session.get(
                ResearchTaskRow,
                task_id,
            )

            if task_row is None:
                return None

            source_rows = list(
                (
                    await session.scalars(
                        select(ResearchSourceRow)
                        .where(ResearchSourceRow.task_id == task_id)
                        .order_by(
                            ResearchSourceRow.retrieved_at,
                            ResearchSourceRow.id,
                        )
                    )
                ).all()
            )

            evidence_rows = list(
                (
                    await session.scalars(
                        select(ResearchEvidenceRow)
                        .where(ResearchEvidenceRow.task_id == task_id)
                        .order_by(
                            ResearchEvidenceRow.created_at,
                            ResearchEvidenceRow.id,
                        )
                    )
                ).all()
            )

            claim_rows = list(
                (
                    await session.scalars(
                        select(ResearchClaimRow)
                        .where(ResearchClaimRow.task_id == task_id)
                        .order_by(ResearchClaimRow.id)
                    )
                ).all()
            )

            claim_ids = [row.id for row in claim_rows]

            claim_evidence_rows: list[ClaimEvidenceRow] = []

            if claim_ids:
                claim_evidence_rows = list(
                    (
                        await session.scalars(
                            select(ClaimEvidenceRow)
                            .where(ClaimEvidenceRow.claim_id.in_(claim_ids))
                            .order_by(
                                ClaimEvidenceRow.claim_id,
                                ClaimEvidenceRow.position,
                            )
                        )
                    ).all()
                )

            evidence_ids_by_claim: dict[
                UUID,
                list[UUID],
            ] = defaultdict(list)

            for row in claim_evidence_rows:
                evidence_ids_by_claim[row.claim_id].append(row.evidence_id)

            report_row = await session.scalar(select(ResearchReportRow).where(ResearchReportRow.task_id == task_id))

            report_claim_ids: list[UUID] = []

            if report_row is not None:
                report_claim_rows = list(
                    (
                        await session.scalars(
                            select(ReportClaimRow)
                            .where(ReportClaimRow.report_id == report_row.id)
                            .order_by(ReportClaimRow.position)
                        )
                    ).all()
                )

                report_claim_ids = [row.claim_id for row in report_claim_rows]

            sources = {
                row.id: Source(
                    source_id=row.id,
                    source_type=SourceType(row.source_type),
                    title=row.title,
                    url=row.url,
                    retrieved_at=row.retrieved_at,
                    content_hash=row.content_hash,
                    metadata=dict(row.source_metadata),
                )
                for row in source_rows
            }

            evidence = {
                row.id: Evidence(
                    evidence_id=row.id,
                    source_id=row.source_id,
                    content=row.content,
                    locator=row.locator,
                    relevance_score=row.relevance_score,
                    created_at=row.created_at,
                )
                for row in evidence_rows
            }

            claims = {
                row.id: Claim(
                    claim_id=row.id,
                    text=row.text,
                    status=ClaimStatus(row.status),
                    evidence_ids=evidence_ids_by_claim[row.id],
                    confidence=row.confidence,
                )
                for row in claim_rows
            }

            report = (
                ResearchReport(
                    report_id=report_row.id,
                    task_id=task_id,
                    title=report_row.title,
                    summary=report_row.summary,
                    claim_ids=report_claim_ids,
                    generated_at=report_row.generated_at,
                )
                if report_row is not None
                else None
            )

            task = ResearchTask(
                task_id=task_row.id,
                query=task_row.query,
                status=ResearchTaskStatus(task_row.status),
                created_at=task_row.created_at,
                updated_at=task_row.updated_at,
                source_ids=[row.id for row in source_rows],
                evidence_ids=[row.id for row in evidence_rows],
                claim_ids=[row.id for row in claim_rows],
                report_id=(report.report_id if report is not None else None),
            )

            return ResearchAggregate(
                task=task,
                sources=sources,
                evidence=evidence,
                claims=claims,
                report=report,
            )
