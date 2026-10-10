"""7B: stage success must have durable receipts, never just a normal return."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from enterprise_research.application.errors import ResearchStageCompletionError
from enterprise_research.application.evidence_extraction_service import ResearchEvidenceExtractionService
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_service import ResearchRunApplicationService
from enterprise_research.application.service import ResearchApplicationService
from enterprise_research.application.stage_completion import (
    EvidenceCompletionManifest,
    ResearchStageCompletionValidator,
    StageArtifactSnapshot,
)
from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
    ResearchRunStatus,
    RetrievedDocument,
    Source,
    SourceType,
)
from enterprise_research.domain.models import utc_now
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import InMemoryResearchPlanRepository


@pytest.mark.parametrize(
    ("stage", "facts", "expected"),
    [
        (ResearchRunStatus.PLANNING, StageArtifactSnapshot(frozenset()), "PLANNING"),
        (ResearchRunStatus.SEARCHING, StageArtifactSnapshot(frozenset({uuid4()})), "SEARCHING"),
        (
            ResearchRunStatus.EXTRACTING,
            StageArtifactSnapshot(frozenset({uuid4()}), web_source_count=1),
            "EXTRACTING",
        ),
        (
            ResearchRunStatus.VERIFYING,
            StageArtifactSnapshot(
                frozenset({uuid4()}),
                web_source_count=1,
                successful_retrieval_count=1,
                manifest_problem="manifest missing",
            ),
            "VERIFYING",
        ),
        (
            ResearchRunStatus.WRITING,
            StageArtifactSnapshot(frozenset({uuid4()})),
            "WRITING",
        ),
    ],
)
def test_stage_proof_fails_closed(stage, facts, expected) -> None:
    with pytest.raises(ResearchStageCompletionError, match=expected):
        ResearchStageCompletionValidator.validate(stage, facts)


def test_complete_lower_stage_proof_succeeds() -> None:
    question_id = uuid4()
    common = StageArtifactSnapshot(
        frozenset({question_id}),
        missing_search_questions=frozenset(),
        web_source_count=1,
        missing_retrieval_sources=frozenset(),
        successful_retrieval_count=1,
        manifest_problem=None,
    )
    for stage in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
    ):
        ResearchStageCompletionValidator.validate(stage, common)


def test_missing_one_search_question_is_not_covered_by_another() -> None:
    q1, q2 = uuid4(), uuid4()
    facts = StageArtifactSnapshot(
        frozenset({q1, q2}),
        missing_search_questions=frozenset({q2}),
        web_source_count=1,
    )
    with pytest.raises(ResearchStageCompletionError, match=str(q2)):
        ResearchStageCompletionValidator.validate(ResearchRunStatus.SEARCHING, facts)


def make_manifest(**overrides) -> EvidenceCompletionManifest:
    values = dict(
        task_id=uuid4(),
        run_id=uuid4(),
        fencing_token=3,
        eligible_document_count=1,
        scheduled_chunk_count=1,
        completed_chunk_count=1,
        verified_count=0,
        created_count=0,
        reused_count=0,
        document_hashes=((uuid4(), "a" * 64),),
        evidence_ids=(),
        evidence_hashes=(),
    )
    values.update(overrides)
    return EvidenceCompletionManifest(**values)


def test_zero_accepted_candidates_is_a_valid_completed_workload() -> None:
    manifest = make_manifest()
    assert manifest.completed_chunk_count == manifest.scheduled_chunk_count
    assert manifest.evidence_ids == ()


@pytest.mark.parametrize(
    "updates",
    [
        {"scheduled_chunk_count": 0, "completed_chunk_count": 0},
        {"completed_chunk_count": 0},
        {"eligible_document_count": 0},
        {"fencing_token": 0},
        {"verified_count": 0, "created_count": 1},
        {"verified_count": 0, "evidence_ids": (uuid4(),)},
        {"verified_count": 1, "evidence_ids": (uuid4(),), "evidence_hashes": ()},
        {"document_hashes": ()},
        {"document_hashes": ((uuid4(), "x"),)},
    ],
)
def test_invalid_manifest_cannot_claim_completion(updates) -> None:
    with pytest.raises(ValueError):
        make_manifest(**updates)


class RecordingManifestRepository:
    def __init__(self) -> None:
        self.saved: list[EvidenceCompletionManifest] = []

    async def save_guarded(self, manifest: EvidenceCompletionManifest, lease: ResearchRunLease) -> None:
        assert manifest.run_id == lease.run_id
        assert manifest.fencing_token == lease.fencing_token
        self.saved.append(manifest)


def test_zero_candidate_extraction_writes_fenced_receipt_after_processing() -> None:
    async def exercise():
        research = InMemoryResearchRepository()
        run_repo = InMemoryResearchRunRepository()
        plan_repo = InMemoryResearchPlanRepository()
        documents = InMemoryRetrievedDocumentRepository()
        app = ResearchApplicationService(research)
        runs = ResearchRunApplicationService(research, run_repo)
        aggregate = await app.create_task("7B legitimate zero evidence")
        source = Source(
            source_type=SourceType.WEB,
            title="Verified document",
            url="https://example.org/zero-evidence",
        )
        await app.add_source(aggregate.task.task_id, source)
        run = await runs.create_run(aggregate.task.task_id)
        for stage in (
            ResearchRunStatus.PLANNING,
            ResearchRunStatus.SEARCHING,
            ResearchRunStatus.EXTRACTING,
            ResearchRunStatus.VERIFYING,
        ):
            await runs.advance_run(aggregate.task.task_id, run.run_id, stage)
        await plan_repo.save(
            ResearchPlan(
                task_id=aggregate.task.task_id,
                run_id=run.run_id,
                objective="Determine if there is relevant evidence",
                questions=[ResearchQuestion(question="Is the result relevant?")],
            )
        )
        await documents.save(
            RetrievedDocument.succeeded(
                source_id=source.source_id,
                requested_url=str(source.url),
                final_url=str(source.url),
                content="A checkable document with no relevant evidence.",
            )
        )

        class NoEvidenceExtractor:
            def __init__(self):
                self.calls = 0

            async def extract(self, **_):
                self.calls += 1
                return []

        manifest_repo = RecordingManifestRepository()
        extractor = NoEvidenceExtractor()
        service = ResearchEvidenceExtractionService(
            research, run_repo, plan_repo, documents, extractor, manifest_repository=manifest_repo
        )
        # Test double observes emission; the live PostgreSQL test separately
        # verifies fenced writes and the transactionally checked receipt.
        lease = ResearchRunLease(run.run_id, uuid4(), 2, utc_now() + timedelta(minutes=1))
        summary = await service.extract_run(aggregate.task.task_id, run.run_id, lease=lease)
        assert summary.verified_count == 0
        assert summary.scheduled_chunk_count >= 1
        assert extractor.calls == summary.scheduled_chunk_count
        assert len(manifest_repo.saved) == 1
        receipt = manifest_repo.saved[0]
        assert receipt.evidence_ids == ()
        assert receipt.completed_chunk_count == summary.scheduled_chunk_count
        assert receipt.document_hashes

    asyncio.run(exercise())


def test_memory_run_repository_rejects_unavailable_atomic_proof() -> None:
    # The memory adapter cannot claim to have PostgreSQL's transactional
    # durability guarantees. The verifier flag must fail explicitly.
    async def exercise():
        from enterprise_research.application import ResearchApplicationService, ResearchRunApplicationService
        from enterprise_research.application.stage_journal import ResearchStageOutcome
        from enterprise_research.infrastructure import InMemoryResearchRepository

        research = InMemoryResearchRepository()
        repo = InMemoryResearchRunRepository()
        app = ResearchRunApplicationService(research, repo)
        task = await ResearchApplicationService(research).create_task("7B: memory is not durable")
        run = await app.create_run(task.task.task_id)
        lease = await app.claim_run_lease(task.task.task_id, run.run_id, uuid4(), ttl_seconds=30)
        assert lease is not None
        await app.advance_run(task.task.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        await app.start_stage(task.task.task_id, run.run_id, ResearchRunStatus.PLANNING, lease=lease)
        with pytest.raises(ResearchStageCompletionError, match="In-memory"):
            await app.finish_stage(
                task.task.task_id,
                run.run_id,
                ResearchRunStatus.PLANNING,
                lease=lease,
                outcome=ResearchStageOutcome.COMPLETED,
                require_artifact_proof=True,
            )

    asyncio.run(exercise())
