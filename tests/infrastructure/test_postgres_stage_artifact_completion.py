"""7B: live PostgreSQL receipt checks must share the fenced Journal transaction.

Opt in only on the local migrated enterprise_research DEVELOPMENT database.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
)
from enterprise_research.application.errors import ResearchStageCompletionError
from enterprise_research.application.evidence_extraction import EvidenceCandidate
from enterprise_research.application.evidence_extraction_service import ResearchEvidenceExtractionService
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import (
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
from enterprise_research.infrastructure.postgres.models import ResearchEvidenceRow, ResearchSourceDocumentRow
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1" or os.getenv("ENTERPRISE_RESEARCH_STAGE_PROOF_TESTS") != "1",
    reason="Requires explicit local PostgreSQL 7B flags and schema migrated to c4e7b0a6d9f2.",
)


def _dev_settings():
    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"} or settings.database != "enterprise_research":
        pytest.fail("7B live DB tests are restricted to the known local enterprise_research DEVELOPMENT database.")
    return settings


async def _create(sessions, name):
    research = PostgresResearchRepository(sessions)
    run_repo = PostgresResearchRunRepository(sessions)
    docs = PostgresRetrievedDocumentRepository(sessions)
    plans = PostgresResearchPlanRepository(sessions)
    manifest_repo = PostgresResearchEvidenceManifestRepository(sessions)
    app = ResearchApplicationService(research)
    runs = ResearchRunApplicationService(research, run_repo)
    aggregate = await app.create_task(name)
    run = await runs.create_run(aggregate.task.task_id)
    lease = await runs.claim_run_lease(aggregate.task.task_id, run.run_id, uuid4(), ttl_seconds=30)
    assert lease is not None
    return research, run_repo, docs, plans, manifest_repo, runs, aggregate, run, lease


async def _enter(runs, task_id, run_id, lease: ResearchRunLease, stage):
    await runs.advance_run(task_id, run_id, stage, lease=lease)
    await runs.start_stage(task_id, run_id, stage, lease=lease)


async def _finish_proven(runs, task_id, run_id, lease: ResearchRunLease, stage):
    return await runs.finish_stage(
        task_id,
        run_id,
        stage,
        lease=lease,
        outcome=ResearchStageOutcome.COMPLETED,
        require_artifact_proof=True,
    )


async def _cleanup(sessions, aggregate):
    async with sessions() as session:
        async with session.begin():
            await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == aggregate.task.task_id))


def test_postgres_proof_is_atomic_with_journal_for_plan_search_retrieval_and_zero_evidence() -> None:
    async def exercise():
        engine = build_async_engine(_dev_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            research, run_repo, docs, plans, manifest_repo, runs, aggregate, run, lease = await _create(
                sessions, "7B atomic proof / zero evidence"
            )
            task_id, run_id = aggregate.task.task_id, run.run_id

            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)
            with pytest.raises(ResearchStageCompletionError, match="PLANNING"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
            question = ResearchQuestion(question="What is the documented behavior?")
            plan = ResearchPlan(task_id=task_id, run_id=run_id, objective="7B evidence", questions=[question])
            await plans.save_guarded(plan, lease, run_repository=run_repo)
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)

            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
            with pytest.raises(ResearchStageCompletionError, match="SEARCHING"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
            source = Source(
                source_type=SourceType.WEB,
                title="A durable source",
                url="https://example.org/verified",
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
            aggregate.add_source(source)
            await research.save_guarded(aggregate, lease, ResearchRunStatus.SEARCHING, run_repository=run_repo)
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)

            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
            with pytest.raises(ResearchStageCompletionError, match="EXTRACTING"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
            doc = RetrievedDocument.succeeded(
                source_id=source.source_id,
                requested_url=str(source.url),
                final_url=str(source.url),
                content="Content that produced no eligible claims in the extraction workload.",
            )
            await docs.save_guarded(doc, lease, task_id=task_id, run_repository=run_repo, research_repository=research)
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)

            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            with pytest.raises(ResearchStageCompletionError, match="VERIFYING"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)

            class NoEvidenceExtractor:
                async def extract(self, **_):
                    return []

            extraction = ResearchEvidenceExtractionService(
                research,
                run_repo,
                plans,
                docs,
                NoEvidenceExtractor(),
                manifest_repository=manifest_repo,
            )
            summary = await extraction.extract_run(task_id, run_id, lease=lease)
            assert summary.verified_count == 0 and summary.scheduled_chunk_count > 0
            # Mutating an already recorded business artifact must invalidate
            # the proof, and must NOT commit a successful journal attempt.
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchSourceDocumentRow)
                        .where(ResearchSourceDocumentRow.source_id == source.source_id)
                        .values(content_hash="0" * 64)
                    )
            with pytest.raises(ResearchStageCompletionError, match="EXTRACTING"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchSourceDocumentRow)
                        .where(ResearchSourceDocumentRow.source_id == source.source_id)
                        .values(content_hash=doc.content_hash)
                    )
            completed = await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            assert completed.outcome == ResearchStageOutcome.COMPLETED
            attempts = await runs.inspect_stage_attempts(task_id, run_id)
            assert len(attempts) == 4
            assert all(x.outcome == ResearchStageOutcome.COMPLETED for x in attempts)
        finally:
            if aggregate is not None:
                await _cleanup(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_executor_rejects_noop_runner_without_persisted_artifact() -> None:
    async def exercise():
        engine = build_async_engine(_dev_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        executor = None
        try:
            research = PostgresResearchRepository(sessions)
            repo = PostgresResearchRunRepository(sessions)
            runs = ResearchRunApplicationService(research, repo)
            aggregate = await ResearchApplicationService(research).create_task("7B no-op stage proof")
            run = await runs.create_run(aggregate.task.task_id)

            class NoopRunner:
                async def run_stage(self, *_args, **_kwargs):
                    return None

            executor = ResearchRunExecutor(runs, NoopRunner(), enforce_artifact_proof=True)
            executor.start(aggregate.task.task_id, run.run_id)

            async def wait_idle():
                while executor.active_count:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(wait_idle(), timeout=10)
            final = await runs.get_run(aggregate.task.task_id, run.run_id)
            assert final.status == ResearchRunStatus.FAILED
            assert "PLANNING proof" in final.error
            attempts = await runs.inspect_stage_attempts(aggregate.task.task_id, run.run_id)
            assert [(entry.stage, entry.outcome) for entry in attempts] == [
                (ResearchRunStatus.PLANNING, ResearchStageOutcome.FAILED)
            ]
        finally:
            if executor is not None:
                await executor.shutdown()
            if aggregate is not None:
                await _cleanup(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_evidence_fingerprint_rejects_mutation_after_receipt() -> None:
    async def exercise():
        engine = build_async_engine(_dev_settings())
        sessions = build_session_factory(engine)
        aggregate = None
        try:
            research, run_repo, docs, plans, manifest_repo, runs, aggregate, run, lease = await _create(
                sessions, "7B evidence content hash tampering"
            )
            task_id, run_id = aggregate.task.task_id, run.run_id
            question = ResearchQuestion(question="What is the precise target?")
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)
            await plans.save_guarded(
                ResearchPlan(task_id=task_id, run_id=run_id, objective="Evidence digest", questions=[question]),
                lease,
                run_repository=run_repo,
            )
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.PLANNING)

            source = Source(
                source_type=SourceType.WEB,
                title="Evidence article",
                url="https://example.org/evidence",
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
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
            aggregate.add_source(source)
            await research.save_guarded(aggregate, lease, ResearchRunStatus.SEARCHING, run_repository=run_repo)
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.SEARCHING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
            await docs.save_guarded(
                RetrievedDocument.succeeded(
                    source_id=source.source_id,
                    requested_url=str(source.url),
                    final_url=str(source.url),
                    content="The target is supported by this exact recorded source.",
                ),
                lease,
                task_id=task_id,
                run_repository=run_repo,
                research_repository=research,
            )
            await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.EXTRACTING)
            await _enter(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)

            class TargetEvidenceExtractor:
                async def extract(self, **_):
                    return [EvidenceCandidate(content="target", relevance_score=1.0)]

            extraction = ResearchEvidenceExtractionService(
                research,
                run_repo,
                plans,
                docs,
                TargetEvidenceExtractor(),
                manifest_repository=manifest_repo,
            )
            summary = await extraction.extract_run(task_id, run_id, lease=lease)
            assert summary.verified_count == summary.created_count == 1
            loaded = await research.get(task_id)
            assert loaded is not None and len(loaded.evidence) == 1
            evidence = next(iter(loaded.evidence.values()))

            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchEvidenceRow)
                        .where(ResearchEvidenceRow.id == evidence.evidence_id)
                        .values(content="tampered")
                    )
            with pytest.raises(ResearchStageCompletionError, match="fingerprint"):
                await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)
            assert (await runs.inspect_stage_attempts(task_id, run_id))[-1].outcome == ResearchStageOutcome.STARTED
            async with sessions() as session:
                async with session.begin():
                    await session.execute(
                        update(ResearchEvidenceRow)
                        .where(ResearchEvidenceRow.id == evidence.evidence_id)
                        .values(content="target")
                    )
            assert (await _finish_proven(runs, task_id, run_id, lease, ResearchRunStatus.VERIFYING)).outcome == (
                ResearchStageOutcome.COMPLETED
            )
        finally:
            if aggregate is not None:
                await _cleanup(sessions, aggregate)
            await engine.dispose()

    asyncio.run(exercise())
