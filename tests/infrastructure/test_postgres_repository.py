from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import delete

from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    ResearchAggregate,
    ResearchTaskStatus,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)

RUN_DB_TESTS = os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason=("Set ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 to run PostgreSQL integration tests."),
)


def test_postgres_repository_round_trip() -> None:
    async def run() -> None:
        settings = PostgresSettings.from_env()

        engine = build_async_engine(settings)
        session_factory = build_session_factory(engine)

        repository = PostgresResearchRepository(session_factory)

        aggregate = ResearchAggregate.create("How does LangGraph durable execution work?")

        source = aggregate.add_source(
            Source(
                source_type=SourceType.WEB,
                title="LangGraph Documentation",
                url=("https://docs.langchain.com/oss/python/langgraph"),
                metadata={
                    "provider": "postgres-test",
                },
            )
        )

        evidence = aggregate.add_evidence(
            Evidence(
                source_id=source.source_id,
                content=("LangGraph supports durable execution."),
                locator="section: durable-execution",
                relevance_score=0.95,
            )
        )

        claim = aggregate.add_claim(
            Claim(
                text=("LangGraph supports durable execution."),
            )
        )

        supported = aggregate.support_claim(
            claim.claim_id,
            [evidence.evidence_id],
            confidence=0.95,
        )

        report = aggregate.build_report(
            title="LangGraph Durable Execution",
            summary="Verified research report.",
        )

        task_id = aggregate.task.task_id

        try:
            await repository.save(aggregate)

            loaded = await repository.get(task_id)

            assert loaded is not None
            assert loaded.task.task_id == task_id
            assert loaded.task.query == ("How does LangGraph durable execution work?")
            assert loaded.task.status == ResearchTaskStatus.COMPLETED

            assert len(loaded.sources) == 1
            assert len(loaded.evidence) == 1
            assert len(loaded.claims) == 1

            loaded_source = loaded.sources[source.source_id]

            assert loaded_source.title == ("LangGraph Documentation")
            assert loaded_source.metadata["provider"] == "postgres-test"

            loaded_evidence = loaded.evidence[evidence.evidence_id]

            assert loaded_evidence.source_id == (source.source_id)
            assert loaded_evidence.relevance_score == 0.95

            loaded_claim = loaded.claims[supported.claim_id]

            assert loaded_claim.status == ClaimStatus.SUPPORTED
            assert loaded_claim.evidence_ids == [evidence.evidence_id]
            assert loaded_claim.confidence == 0.95

            lineage = loaded.trace_claim(loaded_claim.claim_id)

            assert len(lineage) == 1
            assert lineage[0][0].evidence_id == evidence.evidence_id
            assert lineage[0][1].source_id == source.source_id

            assert loaded.report is not None
            assert loaded.report.report_id == (report.report_id)
            assert loaded.report.claim_ids == [supported.claim_id]
            assert loaded.task.report_id == (report.report_id)

            loaded.task.query = "Updated durable execution research"

            await repository.save(loaded)

            reloaded = await repository.get(task_id)

            assert reloaded is not None
            assert reloaded.task.query == ("Updated durable execution research")
            assert len(reloaded.sources) == 1
            assert len(reloaded.evidence) == 1
            assert len(reloaded.claims) == 1
            assert reloaded.report is not None

        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == task_id))

            await engine.dispose()

    asyncio.run(run())
