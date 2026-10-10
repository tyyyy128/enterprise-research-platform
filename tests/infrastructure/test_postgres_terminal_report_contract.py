"""7C: opt-in local PostgreSQL final report and terminal transition verification."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from enterprise_research.application import ResearchApplicationService, ResearchRunApplicationService
from enterprise_research.application.errors import ResearchReportWritingError
from enterprise_research.application.evidence_extraction import EvidenceCandidate
from enterprise_research.application.evidence_extraction_service import ResearchEvidenceExtractionService
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    ResearchPlan,
    ResearchQuestion,
    ResearchRunStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresRetrievedDocumentRepository,
    PostgresSettings,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.evidence_manifest_repository import (
    PostgresResearchEvidenceManifestRepository,
)
from enterprise_research.infrastructure.postgres.models import ResearchClaimRow, ResearchRunReportRow
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository
from enterprise_research.infrastructure.postgres.report_artifacts import PostgresResearchRunReportRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
    or os.getenv("ENTERPRISE_RESEARCH_TERMINAL_PROOF_TESTS") != "1",
    reason="Explicit local PostgreSQL 7C flags required; database migration must be ea7c2f19b540.",
)


def _settings():
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("7C database tests require the local enterprise_research development database.")
    return settings


async def _entered(runs, task_id, run_id, lease, stage):
    await runs.advance_run(task_id, run_id, stage, lease=lease)
    await runs.start_stage(task_id, run_id, stage, lease=lease)


async def _complete(runs, task_id, run_id, lease, stage):
    await runs.finish_stage(
        task_id,
        run_id,
        stage,
        lease=lease,
        outcome=ResearchStageOutcome.COMPLETED,
        require_artifact_proof=True,
    )


async def _build_verified_run(sessions, *, with_supported_claim=True):
    research = PostgresResearchRepository(sessions)
    run_repo = PostgresResearchRunRepository(sessions)
    runs = ResearchRunApplicationService(research, run_repo)
    plan_repo = PostgresResearchPlanRepository(sessions)
    document_repo = PostgresRetrievedDocumentRepository(sessions)
    manifest_repo = PostgresResearchEvidenceManifestRepository(sessions)
    report_repo = PostgresResearchRunReportRepository(sessions)
    aggregate = await ResearchApplicationService(research).create_task("7C scoped report")
    run = await runs.create_run(aggregate.task.task_id)
    task_id, run_id = aggregate.task.task_id, run.run_id
    lease = await runs.claim_run_lease(task_id, run_id, uuid4(), ttl_seconds=90)
    assert lease is not None

    question = ResearchQuestion(question="What does the source state?")
    await _entered(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)
    await plan_repo.save_guarded(
        ResearchPlan(task_id=task_id, run_id=run_id, objective="7C verified research", questions=[question]),
        lease,
        run_repository=run_repo,
    )
    await _complete(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)

    source = Source(
        source_type=SourceType.WEB,
        title="Documented source",
        url="https://example.org/7c",
        metadata={
            "search": {
                "hits": [
                    {
                        "run_id": str(run_id),
                        "question_id": str(question.question_id),
                        "query": question.question,
                    }
                ]
            }
        },
    )
    await _entered(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
    aggregate.add_source(source)
    await research.save_guarded(aggregate, lease, ResearchRunStatus.SEARCHING, run_repository=run_repo)
    await _complete(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)

    await _entered(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
    await document_repo.save_guarded(
        RetrievedDocument.succeeded(
            source_id=source.source_id,
            requested_url=str(source.url),
            final_url=str(source.url),
            content="A verified excerpt is present in this durable research source.",
        ),
        lease,
        task_id=task_id,
        run_repository=run_repo,
        research_repository=research,
    )
    await _complete(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)

    await _entered(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)

    class Extractor:
        async def extract(self, **_):
            return [EvidenceCandidate(content="verified excerpt", relevance_score=1.0)]

    extraction = ResearchEvidenceExtractionService(
        research,
        run_repo,
        plan_repo,
        document_repo,
        Extractor(),
        manifest_repository=manifest_repo,
    )
    summary = await extraction.extract_run(task_id, run_id, lease=lease)
    assert summary.verified_count == summary.created_count == 1
    if with_supported_claim:
        refreshed = await research.get(task_id)
        assert refreshed is not None
        evidence_id = next(iter(refreshed.evidence))
        refreshed.add_claim(
            Claim(
                text="The source contains a verifiable excerpt.",
                status=ClaimStatus.SUPPORTED,
                confidence=0.9,
                evidence_ids=[evidence_id],
            )
        )
        await research.save_guarded(refreshed, lease, ResearchRunStatus.VERIFYING, run_repository=run_repo)

    await _complete(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
    await _entered(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
    return research, runs, report_repo, aggregate, run, lease


def test_postgres_report_write_once_and_atomic_terminal_proof():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            _, runs, reports, aggregate, run, lease = await _build_verified_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            with pytest.raises(ResearchReportWritingError, match="artifact"):
                await _complete(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)

            artifact = await reports.write_guarded(task_id, run_id, lease)
            assert artifact.citations and artifact.title == "7C verified research"
            assert "verified excerpt" in artifact.body
            assert await reports.get(task_id, run_id) == artifact
            assert await reports.get(uuid4(), run_id) is None
            with pytest.raises(ResearchReportWritingError, match="immutable"):
                await reports.write_guarded(task_id, run_id, lease)

            # Terminal transition cannot bypass a completed WRITING journal.
            with pytest.raises(ResearchReportWritingError, match="journal"):
                await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.WRITING

            # If the report itself changed, its proof must roll back Journal completion.
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchRunReportRow)
                        .where(ResearchRunReportRow.run_id == run_id)
                        .values(body="tampered")
                    )
            with pytest.raises(ResearchReportWritingError, match="changed"):
                await _complete(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchRunReportRow)
                        .where(ResearchRunReportRow.run_id == run_id)
                        .values(body=artifact.body)
                    )
            await _complete(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)

            # A supported claim changing AFTER writing but BEFORE terminal
            # also invalidates the freshly stored report.
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.task_id == task_id)
                        .values(text="unverified mutation")
                    )
            with pytest.raises(ResearchReportWritingError, match="changed"):
                await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.WRITING
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.task_id == task_id)
                        .values(text="The source contains a verifiable excerpt.")
                    )
            await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.COMPLETED
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_missing_supported_claim_never_writes_report():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            _, runs, reports, aggregate, run, lease = await _build_verified_run(sessions, with_supported_claim=False)
            task_id, run_id = aggregate.task.task_id, run.run_id
            with pytest.raises(ResearchReportWritingError, match="supported, evidence-verified Claim"):
                await reports.write_guarded(task_id, run_id, lease)
            assert await reports.get(task_id, run_id) is None
            with pytest.raises(ResearchReportWritingError, match="artifact"):
                await _complete(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.WRITING
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())
