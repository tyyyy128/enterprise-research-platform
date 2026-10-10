"""3H opt-in REAL PostgreSQL fenced synthesis/terminal regression.

Requires a local development database migrated to c8d4a762e901.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import update
from test_postgres_claim_verification import (
    Assessor,
    _clear,
    _enter,
    _finish,
    _generated,
    _prepared_run,
)
from test_postgres_terminal_report_contract import _build_verified_run

from enterprise_research.application.claim_generation import ClaimCandidate, ResearchClaimGenerationService
from enterprise_research.application.claim_verification import (
    ClaimVerificationVerdict,
    EvidenceQuote,
    ResearchClaimVerificationService,
)
from enterprise_research.application.errors import ResearchReportWritingError
from enterprise_research.application.report_synthesis import (
    ReportSynthesisPlan,
    ReportSynthesisSection,
    ResearchReportSynthesisService,
)
from enterprise_research.application.run_lease import ResearchRunLeaseLostError
from enterprise_research.domain import ClaimStatus, ResearchRunStatus
from enterprise_research.infrastructure.postgres import PostgresSettings, build_async_engine, build_session_factory
from enterprise_research.infrastructure.postgres.claim_generation import PostgresResearchClaimGenerationRepository
from enterprise_research.infrastructure.postgres.claim_verification import PostgresResearchClaimVerificationRepository
from enterprise_research.infrastructure.postgres.models import (
    ResearchClaimRow,
    ResearchRunReportRow,
    ResearchRunReportSynthesisRow,
)
from enterprise_research.infrastructure.postgres.report_artifacts import PostgresResearchRunReportRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_SYNTHESIS_TESTS") != "1",
    reason="Explicit local PostgreSQL 3H flags are required, and c8d4a762e901 must be migrated.",
)


def _settings():
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("3H acceptance tests require local enterprise_research development database.")
    return settings


class Outline:
    def __init__(self, during_model=None):
        self.during_model = during_model

    async def compose(self, ctx):
        if self.during_model is not None:
            await self.during_model(ctx)
        ids = tuple(c.claim_id for c in ctx.claims)
        return ReportSynthesisPlan(ids[:1], (ReportSynthesisSection("Key findings", ids),))


async def _verified_writing(sessions):
    runs, aggregate, run, lease, receipt = await _generated(sessions)
    task_id, run_id = aggregate.task.task_id, run.run_id
    verifier = ResearchClaimVerificationService(
        PostgresResearchClaimVerificationRepository(sessions), Assessor(), model="3g-test"
    )
    await verifier.verify_run(task_id, run_id, lease=lease)
    await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
    await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
    return runs, aggregate, run, lease


def test_postgres_3h_report_and_outline_atomic_and_terminal_proven():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _verified_writing(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repository = PostgresResearchRunReportRepository(sessions)
            writer = ResearchReportSynthesisService(repository, Outline(), model="mock-3h")
            await writer.write_run(task_id, run_id, lease=lease)
            artifact = await repository.get(task_id, run_id)
            assert artifact is not None
            assert artifact.synthesis_model == "mock-3h" and len(artifact.synthesis_sha256) == 64
            assert "## Executive overview" in artifact.body
            assert "growth reached 14 percent" in artifact.body.lower()
            async with sessions() as session:
                row = await session.get(ResearchRunReportRow, run_id)
                outline = await session.get(ResearchRunReportSynthesisRow, run_id)
                assert row is not None and outline is not None
                assert outline.plan["sections"][0]["heading"] == "Key findings"
            with pytest.raises(ResearchReportWritingError, match="already"):
                await writer.write_run(task_id, run_id, lease=lease)
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.COMPLETED
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3h_model_time_claim_mutation_writes_no_report_or_receipt():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _verified_writing(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repository = PostgresResearchRunReportRepository(sessions)

            async def mutate(ctx):
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            update(ResearchClaimRow)
                            .where(ResearchClaimRow.id == ctx.claims[0].claim_id)
                            .values(text="modified after LLM input snapshot")
                        )

            with pytest.raises(Exception, match="changed|changed after|mutated|receipt|Claim"):
                await ResearchReportSynthesisService(repository, Outline(mutate), model="mock-3h").write_run(
                    task_id, run_id, lease=lease
                )
            assert await repository.get(task_id, run_id) is None
            async with sessions() as session:
                assert await session.get(ResearchRunReportSynthesisRow, run_id) is None
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3h_revoked_lease_during_model_cannot_write_report():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _verified_writing(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repository = PostgresResearchRunReportRepository(sessions)

            async def release(_):
                assert await runs.release_run_lease(lease)

            with pytest.raises(ResearchRunLeaseLostError):
                await ResearchReportSynthesisService(repository, Outline(release), model="mock-3h").write_run(
                    task_id, run_id, lease=lease
                )
            assert await repository.get(task_id, run_id) is None
            async with sessions() as session:
                assert await session.get(ResearchRunReportSynthesisRow, run_id) is None
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("tamper", ["model", "plan", "body"])
def test_postgres_3h_receipt_tamper_rolls_back_writing_journal(tamper):
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _verified_writing(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id
            repository = PostgresResearchRunReportRepository(sessions)
            await ResearchReportSynthesisService(repository, Outline(), model="mock-3h").write_run(
                task_id, run_id, lease=lease
            )
            async with sessions() as session:
                async with session.begin():
                    if tamper == "model":
                        await session.execute(
                            update(ResearchRunReportSynthesisRow)
                            .where(ResearchRunReportSynthesisRow.run_id == run_id)
                            .values(model="tampered")
                        )
                    elif tamper == "plan":
                        await session.execute(
                            update(ResearchRunReportSynthesisRow)
                            .where(ResearchRunReportSynthesisRow.run_id == run_id)
                            .values(
                                plan={
                                    "highlights": [],
                                    "sections": [{"heading": "Key findings", "claim_ids": [str(uuid4())]}],
                                }
                            )
                        )
                    else:
                        await session.execute(
                            update(ResearchRunReportRow)
                            .where(ResearchRunReportRow.run_id == run_id)
                            .values(body="tampered")
                        )
            with pytest.raises(ResearchReportWritingError, match="receipt|changed|unknown|invented"):
                await _finish(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            assert (await runs.get_run(task_id, run_id)).status == ResearchRunStatus.WRITING
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3h_adverse_verdicts_are_disclosed_not_promoted():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            runs, aggregate, run, lease = await _prepared_run(sessions)
            task_id, run_id = aggregate.task.task_id, run.run_id

            class TwoClaims:
                async def propose(self, context):
                    evidence_id = context.evidence[0].evidence_id
                    return [
                        ClaimCandidate("Growth reached 14 percent in 2025.", (evidence_id,)),
                        ClaimCandidate("This growth proves a permanent trend.", (evidence_id,)),
                    ]

            await ResearchClaimGenerationService(
                PostgresResearchClaimGenerationRepository(sessions), TwoClaims(), model="proposer"
            ).generate_run(task_id, run_id, lease=lease)

            class MixedAssessment:
                async def assess(self, context):
                    return [
                        ClaimVerificationVerdict(
                            context.claims[0].claim_id,
                            ClaimStatus.SUPPORTED,
                            0.96,
                            "Direct passage",
                            (EvidenceQuote(context.evidence[0].evidence_id, "growth reached 14 percent"),),
                        ),
                        ClaimVerificationVerdict(
                            context.claims[1].claim_id,
                            ClaimStatus.UNSUPPORTED,
                            0.2,
                            "No evidence about permanent trends.",
                        ),
                    ]

            await ResearchClaimVerificationService(
                PostgresResearchClaimVerificationRepository(sessions), MixedAssessment(), model="verifier"
            ).verify_run(task_id, run_id, lease=lease)
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            repository = PostgresResearchRunReportRepository(sessions)
            await ResearchReportSynthesisService(repository, Outline(), model="mock-3h").write_run(
                task_id, run_id, lease=lease
            )
            report = await repository.get(task_id, run_id)
            assert report is not None
            assert "## Conflicts and evidence limitations" in report.body
            assert "This growth proves a permanent trend" in report.body
            assert "Insufficient support" in report.body
            assert len(report.citations) == 1  # Unverified candidate is NOT a supported citation.
            await _finish(runs, task_id, run_id, lease, ResearchRunStatus.WRITING)
            await runs.advance_run(task_id, run_id, ResearchRunStatus.COMPLETED, lease=lease)
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_3h_rejects_manual_supported_claim_without_3g_run_receipt():
    async def exercise():
        engine = build_async_engine(_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            _, _, repository, aggregate, run, lease = await _build_verified_run(sessions)
            with pytest.raises(ResearchReportWritingError, match="3F/3G Run-level"):
                await ResearchReportSynthesisService(repository, Outline(), model="mock-3h").write_run(
                    aggregate.task.task_id, run.run_id, lease=lease
                )
            assert await repository.get(aggregate.task.task_id, run.run_id) is None
        finally:
            await _clear(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())
