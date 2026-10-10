"""3F opt-in REAL local PostgreSQL claim provenance/fencing acceptance.

Runs only when both flags are explicitly enabled. Never points at remote DBs.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from enterprise_research.application import ResearchApplicationService, ResearchRunApplicationService
from enterprise_research.application.claim_generation import (
    ClaimCandidate,
    ClaimGenerationLimits,
    ResearchClaimGenerationService,
)
from enterprise_research.application.errors import (
    ResearchClaimGenerationError,
    ResearchReportWritingError,
    ResearchStageCompletionError,
)
from enterprise_research.application.evidence_extraction import EvidenceCandidate
from enterprise_research.application.evidence_extraction_service import ResearchEvidenceExtractionService
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import (
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
from enterprise_research.infrastructure.postgres.claim_generation import PostgresResearchClaimGenerationRepository
from enterprise_research.infrastructure.postgres.evidence_manifest_repository import (
    PostgresResearchEvidenceManifestRepository,
)
from enterprise_research.infrastructure.postgres.models import (
    ClaimEvidenceRow,
    ResearchClaimRow,
    ResearchEvidenceRow,
    ResearchRunClaimGenerationRow,
)
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository
from enterprise_research.infrastructure.postgres.report_artifacts import PostgresResearchRunReportRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_CLAIM_TESTS") != "1",
    reason="Explicit local 3F PostgreSQL flags required; schema must include b6e9f4c0a21d.",
)


def _settings():
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("3F tests require a local enterprise_research development database.")
    return settings


async def _enter(runs, task_id, run_id, lease, stage):
    await runs.advance_run(task_id, run_id, stage, lease=lease)
    await runs.start_stage(task_id, run_id, stage, lease=lease)


async def _finish(runs, task_id, run_id, lease, stage):
    await runs.finish_stage(
        task_id, run_id, stage, lease=lease, outcome=ResearchStageOutcome.COMPLETED, require_artifact_proof=True
    )


async def _prepared_run(sessions, *, zero_evidence=False):
    research = PostgresResearchRepository(sessions)
    run_repo = PostgresResearchRunRepository(sessions)
    runs = ResearchRunApplicationService(research, run_repo)
    plan_repo = PostgresResearchPlanRepository(sessions)
    documents = PostgresRetrievedDocumentRepository(sessions)
    receipts = PostgresResearchEvidenceManifestRepository(sessions)
    aggregate = await ResearchApplicationService(research).create_task("3F candidate proposal")
    run = await runs.create_run(aggregate.task.task_id)
    task_id, run_id = aggregate.task.task_id, run.run_id
    lease = await runs.claim_run_lease(task_id, run_id, uuid4(), ttl_seconds=90)
    assert lease is not None
    question = ResearchQuestion(question="What does the retrieved source establish?")
    await _enter(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)
    await plan_repo.save_guarded(
        ResearchPlan(task_id=task_id, run_id=run_id, objective="3F provenance test", questions=[question]),
        lease,
        run_repository=run_repo,
    )
    await _finish(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)

    source = Source(
        source_type=SourceType.WEB,
        title="Durable origin",
        url="https://example.org/claim-test",
        metadata={
            "search": {
                "hits": [{"run_id": str(run_id), "question_id": str(question.question_id), "query": question.question}]
            }
        },
    )
    await _enter(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
    aggregate.add_source(source)
    await research.save_guarded(aggregate, lease, ResearchRunStatus.SEARCHING, run_repository=run_repo)
    await _finish(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)

    await _enter(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
    await documents.save_guarded(
        RetrievedDocument.succeeded(
            source_id=source.source_id,
            requested_url=str(source.url),
            final_url=str(source.url),
            content="This durable document reports growth reached 14 percent in 2025.",
        ),
        lease,
        task_id=task_id,
        run_repository=run_repo,
        research_repository=research,
    )
    await _finish(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
    await _enter(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)

    class Extractor:
        async def extract(self, **_):
            if zero_evidence:
                return []
            return [EvidenceCandidate(content="growth reached 14 percent", relevance_score=0.91)]

    service = ResearchEvidenceExtractionService(
        research, run_repo, plan_repo, documents, Extractor(), manifest_repository=receipts
    )
    extraction = await service.extract_run(task_id, run_id, lease=lease)
    assert extraction.verified_count == (0 if zero_evidence else 1)
    return runs, aggregate, run, lease


class CandidateGenerator:
    def __init__(self, *, mutate=None, bad_reference=False):
        self.mutate = mutate
        self.bad_reference = bad_reference
        self.calls = 0

    async def propose(self, context):
        self.calls += 1
        if self.mutate is not None:
            await self.mutate(context)
        reference = uuid4() if self.bad_reference else context.evidence[0].evidence_id
        return [ClaimCandidate("The source reports 14 percent growth in 2025.", (reference,))]


def test_postgres_claims_are_unverified_run_scoped_and_write_once():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repo = PostgresResearchClaimGenerationRepository(sessions)
            generator = CandidateGenerator()
            service = ResearchClaimGenerationService(repo, generator, model="mock-claim-v1")
            with pytest.raises(ResearchClaimGenerationError, match="out-of-scope"):
                await ResearchClaimGenerationService(
                    repo, CandidateGenerator(bad_reference=True), model="mock"
                ).generate_run(task_id, run_id, lease=lease)
            receipt = await service.generate_run(task_id, run_id, lease=lease)
            assert receipt.fencing_token == lease.fencing_token and len(receipt.claim_ids) == 1
            assert len(receipt.selected_evidence_ids) == 1 and generator.calls == 1
            async with sessions() as session:
                claim = await session.get(ResearchClaimRow, receipt.claim_ids[0])
                link = await session.scalar(
                    select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id == receipt.claim_ids[0])
                )
                stored = await session.get(ResearchRunClaimGenerationRow, run_id)
                assert claim is not None and claim.status == ClaimStatus.UNSUPPORTED.value and claim.confidence == 0.0
                assert link is not None and link.evidence_id == receipt.selected_evidence_ids[0]
                assert stored is not None and stored.candidate_count == 1
            with pytest.raises(ResearchClaimGenerationError, match="write-once"):
                await service.generate_run(task_id, run_id, lease=lease)
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            with pytest.raises(ResearchReportWritingError, match="supported"):
                await PostgresResearchRunReportRepository(sessions).write_guarded(task_id, run_id, lease)
            assert await runs.release_run_lease(lease)
            with pytest.raises(ResearchRunLeaseLostError):
                await repo.load_context(task_id, run_id, lease, ClaimGenerationLimits())
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_claim_input_mutation_aborts_without_partial_writes():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id

            async def mutate(context):
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            update(ResearchEvidenceRow)
                            .where(ResearchEvidenceRow.id == context.evidence[0].evidence_id)
                            .values(content="tampered after model input")
                        )

            repo = PostgresResearchClaimGenerationRepository(sessions)
            service = ResearchClaimGenerationService(repo, CandidateGenerator(mutate=mutate), model="mock")
            with pytest.raises(ResearchStageCompletionError, match="VERIFYING proof missing|input changed"):
                await service.generate_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                assert await session.get(ResearchRunClaimGenerationRow, run_id) is None
                count = await session.scalar(
                    select(func.count()).select_from(ResearchClaimRow).where(ResearchClaimRow.task_id == task_id)
                )
                assert count == 0
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_zero_evidence_records_empty_receipt_and_stage_can_complete():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions, zero_evidence=True)
            task_id, run_id = aggregate.task.task_id, run.run_id
            generator = CandidateGenerator()
            service = ResearchClaimGenerationService(
                PostgresResearchClaimGenerationRepository(sessions), generator, model="mock"
            )
            receipt = await service.generate_run(task_id, run_id, lease=lease)
            assert not receipt.claim_ids and not receipt.selected_evidence_ids and generator.calls == 0
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            async with sessions() as session:
                row = await session.get(ResearchRunClaimGenerationRow, run_id)
                assert row is not None and row.candidate_count == 0
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_receipt_mutation_rolls_back_verifying_journal():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            service = ResearchClaimGenerationService(
                PostgresResearchClaimGenerationRepository(sessions), CandidateGenerator(), model="mock"
            )
            receipt = await service.generate_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.id == receipt.claim_ids[0])
                        .values(text="tampered proposed claim")
                    )
            with pytest.raises(ResearchClaimGenerationError, match="changed"):
                await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            attempt = (await runs.inspect_stage_attempts(task_id, run_id))[-1]
            assert attempt.stage == ResearchRunStatus.VERIFYING and attempt.outcome == ResearchStageOutcome.STARTED
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_lost_lease_during_model_call_cannot_write_claims():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id

            async def revoke(_context):
                assert await runs.release_run_lease(lease)

            service = ResearchClaimGenerationService(
                PostgresResearchClaimGenerationRepository(sessions),
                CandidateGenerator(mutate=revoke),
                model="mock",
            )
            with pytest.raises(ResearchRunLeaseLostError):
                await service.generate_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                assert await session.get(ResearchRunClaimGenerationRow, run_id) is None
                count = await session.scalar(
                    select(func.count()).select_from(ResearchClaimRow).where(ResearchClaimRow.task_id == task_id)
                )
                assert count == 0
        finally:
            if aggregate is not None:
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id)
                        )
            await engine.dispose()

    asyncio.run(exercise())
