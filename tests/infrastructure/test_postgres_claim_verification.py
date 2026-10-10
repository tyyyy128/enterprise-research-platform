"""3G opt-in REAL local PostgreSQL semantic Claim verification/Fencing acceptance.

Requires migrated b6e9f4c0a21d -> f3a7c9d25168; never targets remote DBs.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from enterprise_research.application import ResearchApplicationService, ResearchRunApplicationService
from enterprise_research.application.claim_generation import ClaimCandidate, ResearchClaimGenerationService
from enterprise_research.application.claim_verification import (
    ClaimVerificationVerdict,
    EvidenceQuote,
    ResearchClaimVerificationService,
)
from enterprise_research.application.errors import (
    ResearchClaimVerificationError,
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
from enterprise_research.infrastructure.postgres.claim_verification import PostgresResearchClaimVerificationRepository
from enterprise_research.infrastructure.postgres.evidence_manifest_repository import (
    PostgresResearchEvidenceManifestRepository,
)
from enterprise_research.infrastructure.postgres.models import (
    ResearchClaimRow,
    ResearchEvidenceRow,
    ResearchRunClaimVerificationRow,
)
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository
from enterprise_research.infrastructure.postgres.report_artifacts import PostgresResearchRunReportRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_VERIFY_CLAIMS_TESTS") != "1",
    reason="3G requires two explicit local DB flags and migration f3a7c9d25168.",
)


def _settings():
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("3G verification tests must use local enterprise_research development database.")
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


class Proposer:
    def __init__(self, *, empty=False):
        self.empty = empty

    async def propose(self, context):
        if self.empty:
            return []
        return [ClaimCandidate("Growth reached 14 percent in 2025.", (context.evidence[0].evidence_id,))]


class Assessor:
    def __init__(self, *, status=ClaimStatus.SUPPORTED, mutate=None):
        self.status, self.mutate, self.calls = status, mutate, 0

    async def assess(self, context):
        self.calls += 1
        if self.mutate is not None:
            await self.mutate(context)
        if self.status == ClaimStatus.SUPPORTED:
            return [
                ClaimVerificationVerdict(
                    context.claims[0].claim_id,
                    ClaimStatus.SUPPORTED,
                    0.94,
                    "The passage directly entails this factual candidate.",
                    (EvidenceQuote(context.evidence[0].evidence_id, "growth reached 14 percent"),),
                )
            ]
        return [
            ClaimVerificationVerdict(
                context.claims[0].claim_id,
                ClaimStatus.UNSUPPORTED,
                0.85,
                "The cited evidence does not establish the full Claim.",
            )
        ]


async def _generated(sessions, *, zero_evidence=False, empty_claims=False):
    runs, aggregate, run, lease = await _prepared_run(sessions, zero_evidence=zero_evidence)
    repo = PostgresResearchClaimGenerationRepository(sessions)
    receipt = await ResearchClaimGenerationService(repo, Proposer(empty=empty_claims), model="3f-mock").generate_run(
        aggregate.task.task_id, run.run_id, lease=lease
    )
    return runs, aggregate, run, lease, receipt


async def _clear(sessions, aggregate):
    if aggregate is None:
        return
    async with sessions() as session:
        async with session.begin():
            await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))


def test_postgres_3g_support_verdict_and_7c_final_report_are_atomic_and_scoped():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease, generated = await _generated(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repo = PostgresResearchClaimVerificationRepository(sessions)
            service = ResearchClaimVerificationService(repo, Assessor(), model="3g-mock")
            verified = await service.verify_run(task_id, run_id, lease=lease)
            assert verified.claim_ids == generated.claim_ids
            async with sessions() as session:
                persisted = await session.get(ResearchClaimRow, generated.claim_ids[0])
                receipt = await session.get(ResearchRunClaimVerificationRow, run_id)
                assert persisted is not None and persisted.status == ClaimStatus.SUPPORTED.value
                assert persisted.confidence == 0.94
                assert receipt is not None and receipt.verdicts[0]["supporting"][0][1] == "growth reached 14 percent"
            with pytest.raises(ResearchClaimVerificationError, match="write-once"):
                await service.verify_run(task_id, run_id, lease=lease)
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            report = await PostgresResearchRunReportRepository(sessions).write_guarded(task_id, run_id, lease)
            assert "Growth reached 14 percent" in report.body
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.COMPLETED
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3g_unsupported_and_empty_decisions_do_not_invent_reports():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregates = []
        try:
            for zero_evidence, empty_claims in [(False, False), (True, False), (False, True)]:
                runs, aggregate, run, lease, receipt = await _generated(
                    sessions, zero_evidence=zero_evidence, empty_claims=empty_claims
                )
                aggregates.append(aggregate)
                task_id, run_id = aggregate.task.task_id, run.run_id
                model = Assessor(status=ClaimStatus.UNSUPPORTED)
                result = await ResearchClaimVerificationService(
                    PostgresResearchClaimVerificationRepository(sessions), model, model="3g-mock"
                ).verify_run(task_id, run_id, lease=lease)
                assert result.claim_ids == receipt.claim_ids
                assert model.calls == (0 if not receipt.claim_ids else 1)
                async with sessions() as session:
                    persisted = await session.get(ResearchRunClaimVerificationRow, run_id)
                    assert persisted is not None and persisted.candidate_count == len(receipt.claim_ids)
                    if receipt.claim_ids:
                        candidate = await session.get(ResearchClaimRow, receipt.claim_ids[0])
                        assert candidate.status == ClaimStatus.UNSUPPORTED.value and candidate.confidence == 0.0
                await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
                await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
                with pytest.raises(ResearchReportWritingError, match="supported|verified"):
                    await PostgresResearchRunReportRepository(sessions).write_guarded(task_id, run_id, lease)
        finally:
            for aggregate in aggregates:
                await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3g_evidence_mutation_during_model_fails_without_partial_updates():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease, generated = await _generated(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id

            async def mutate(context):
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            update(ResearchEvidenceRow)
                            .where(ResearchEvidenceRow.id == context.evidence[0].evidence_id)
                            .values(content="tampered evidence during verification model")
                        )

            service = ResearchClaimVerificationService(
                PostgresResearchClaimVerificationRepository(sessions), Assessor(mutate=mutate), model="3g-mock"
            )
            with pytest.raises(ResearchStageCompletionError, match="proof missing|changed|fingerprint"):
                await service.verify_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                assert await session.get(ResearchRunClaimVerificationRow, run_id) is None
                candidate = await session.get(ResearchClaimRow, generated.claim_ids[0])
                assert candidate.status == ClaimStatus.UNSUPPORTED.value and candidate.confidence == 0.0
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3g_lost_lease_during_model_call_prevents_promotions():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease, generated = await _generated(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id

            async def revoke(_):
                assert await runs.release_run_lease(lease)

            with pytest.raises(ResearchRunLeaseLostError):
                await ResearchClaimVerificationService(
                    PostgresResearchClaimVerificationRepository(sessions), Assessor(mutate=revoke), model="3g-mock"
                ).verify_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                assert await session.get(ResearchRunClaimVerificationRow, run_id) is None
                candidate = await session.get(ResearchClaimRow, generated.claim_ids[0])
                assert candidate.status == ClaimStatus.UNSUPPORTED.value and candidate.confidence == 0.0
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3g_claim_or_receipt_mutation_blocks_verifying_completion():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease, generated = await _generated(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            await ResearchClaimVerificationService(
                PostgresResearchClaimVerificationRepository(sessions), Assessor(), model="3g-mock"
            ).verify_run(task_id, run_id, lease=lease)
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.id == generated.claim_ids[0])
                        .values(status=ClaimStatus.UNSUPPORTED.value)
                    )
            with pytest.raises(ResearchClaimVerificationError, match="status/confidence"):
                await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.id == generated.claim_ids[0])
                        .values(status=ClaimStatus.SUPPORTED.value)
                    )
                    await session.execute(
                        update(ResearchRunClaimVerificationRow)
                        .where(ResearchRunClaimVerificationRow.run_id == run_id)
                        .values(verdicts_sha256="0" * 64)
                    )
            with pytest.raises(ResearchClaimVerificationError, match="digest"):
                await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3g_manual_supported_candidate_cannot_bypass_missing_verifier():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease, generated = await _generated(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchClaimRow)
                        .where(ResearchClaimRow.id == generated.claim_ids[0])
                        .values(status=ClaimStatus.SUPPORTED.value, confidence=0.98)
                    )
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            with pytest.raises(ResearchClaimVerificationError, match="receipt missing"):
                await PostgresResearchRunReportRepository(sessions).write_guarded(task_id, run_id, lease)
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())
