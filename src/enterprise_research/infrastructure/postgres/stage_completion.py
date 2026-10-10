"""SQL-backed Stage Artifact Completion Proof, locked with Journal completion.

The caller MUST hold the leased Run row lock in this very transaction.
The Task row and inspected business rows are locked against concurrent
mutations while the current Stage Journal is marked COMPLETED. This cannot
make external API side effects transactional; it only proves local artifacts.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_research.application.errors import ResearchStageCompletionError
from enterprise_research.application.stage_completion import (
    ResearchStageCompletionValidator,
    StageArtifactSnapshot,
    evidence_fingerprint,
)
from enterprise_research.domain import ResearchRunStatus, RetrievalStatus, SourceType
from enterprise_research.infrastructure.postgres.claim_generation_receipt import validate_run_claim_receipt
from enterprise_research.infrastructure.postgres.claim_verification import validate_run_claim_verification_receipt
from enterprise_research.infrastructure.postgres.models import (
    ResearchEvidenceManifestRow,
    ResearchEvidenceRow,
    ResearchPlanQuestionRow,
    ResearchPlanRow,
    ResearchSourceDocumentRow,
    ResearchSourceRow,
    ResearchTaskRow,
)


async def validate_persisted_stage_artifacts(
    session: AsyncSession,
    *,
    task_id: UUID,
    run_id: UUID,
    stage: ResearchRunStatus,
    fencing_token: int,
) -> None:
    """Verify durable receipts while the caller still holds the Run lock."""
    if stage == ResearchRunStatus.WRITING:
        # 7C: the writer's durable report must match live run-scoped claims.
        # Imported locally to avoid a cycle: report completion reuses the
        # existing VERIFYING business artifact checks.
        from enterprise_research.infrastructure.postgres.report_artifacts import validate_run_terminal_report

        await validate_run_terminal_report(
            session,
            task_id=task_id,
            run_id=run_id,
            fencing_token=fencing_token,
            require_writing_journal=False,
        )
        return

    # Serializes against administrative aggregate saves, which update the
    # shared task row before rewriting children. Fenced writers also lock Run
    # first; no lock-order inversion is introduced by this read.
    task = await session.scalar(select(ResearchTaskRow.id).where(ResearchTaskRow.id == task_id).with_for_update())
    if task is None:
        raise ResearchStageCompletionError("Task does not exist for this stage completion proof.")

    plan = await session.scalar(
        select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id).with_for_update(read=True)
    )
    if plan is None or not plan.objective.strip():
        raise ResearchStageCompletionError("PLANNING proof missing: persisted plan/objective.")
    questions = (
        await session.scalars(
            select(ResearchPlanQuestionRow)
            .where(ResearchPlanQuestionRow.plan_id == plan.id)
            .order_by(ResearchPlanQuestionRow.position)
            .with_for_update(read=True)
        )
    ).all()
    if not questions or any(not question.question.strip() for question in questions):
        raise ResearchStageCompletionError("PLANNING proof missing: nonempty persisted questions.")
    question_keys = {(question.id, question.question) for question in questions}
    if len({q.casefold().strip() for _, q in question_keys}) != len(questions):
        raise ResearchStageCompletionError("PLANNING proof invalid: duplicate question text.")
    if stage == ResearchRunStatus.PLANNING:
        ResearchStageCompletionValidator.validate(
            stage, StageArtifactSnapshot(plan_question_ids=frozenset(question.id for question in questions))
        )
        return

    # Every planned question needs run-scoped web source provenance, even if
    # a source was originally discovered by another run for the same task.
    all_sources = (
        await session.scalars(
            select(ResearchSourceRow)
            .where(ResearchSourceRow.task_id == task_id)
            .order_by(ResearchSourceRow.id)
            .with_for_update(read=True)
        )
    ).all()
    covered: set[UUID] = set()
    source_by_id = {}
    for source in all_sources:
        if source.source_type != SourceType.WEB.value:
            continue
        if not source.url or not source.url.strip():
            continue
        source_by_id[source.id] = source
        search = source.source_metadata.get("search")
        hits = search.get("hits") if isinstance(search, dict) else None
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict) or hit.get("run_id") != str(run_id):
                continue
            for qid, query in question_keys:
                if hit.get("question_id") == str(qid) and hit.get("query") == query:
                    covered.add(qid)

    missing_questions = frozenset(question.id for question in questions if question.id not in covered)
    base = dict(
        plan_question_ids=frozenset(question.id for question in questions),
        missing_search_questions=missing_questions,
        web_source_count=len(source_by_id),
    )
    if stage == ResearchRunStatus.SEARCHING:
        ResearchStageCompletionValidator.validate(stage, StageArtifactSnapshot(**base))
        return

    document_rows = (
        (
            await session.scalars(
                select(ResearchSourceDocumentRow)
                .where(ResearchSourceDocumentRow.source_id.in_([source.id for source in all_sources]))
                .with_for_update(read=True)
            )
        ).all()
        if all_sources
        else []
    )
    docs = {doc.source_id: doc for doc in document_rows}
    missing_sources: set[UUID] = set()
    successful: dict[UUID, ResearchSourceDocumentRow] = {}
    for source_id, source in source_by_id.items():
        document = docs.get(source_id)
        if document is None or document.requested_url != source.url:
            missing_sources.add(source_id)
        elif document.status == RetrievalStatus.SUCCEEDED.value:
            if (
                not document.content
                or not document.content.strip()
                or not document.content_hash
                or document.content_hash != hashlib.sha256(document.content.encode("utf-8")).hexdigest()
            ):
                missing_sources.add(source_id)
            else:
                successful[source_id] = document
        elif document.status == RetrievalStatus.FAILED.value:
            if (
                not isinstance(document.document_metadata, dict)
                or (document.document_metadata.get("retrieval_run_id") != str(run_id))
                or not document.error
            ):
                missing_sources.add(source_id)
        else:
            missing_sources.add(source_id)
    base.update(
        missing_retrieval_sources=frozenset(missing_sources),
        successful_retrieval_count=len(successful),
    )
    if stage == ResearchRunStatus.EXTRACTING:
        ResearchStageCompletionValidator.validate(stage, StageArtifactSnapshot(**base))
        return

    # The extractor considers all available task sources, including future
    # non-web source types. Its receipt therefore inventories ALL successful
    # documents, not just the web documents validated by EXTRACTING.
    eligible_docs: dict[UUID, ResearchSourceDocumentRow] = {}
    invalid_success = False
    for source in all_sources:
        doc = docs.get(source.id)
        if doc is None or doc.status != RetrievalStatus.SUCCEEDED.value:
            continue
        if (
            (source.url is not None and doc.requested_url != source.url)
            or not doc.content
            or not doc.content.strip()
            or not doc.content_hash
            or doc.content_hash != hashlib.sha256(doc.content.encode("utf-8")).hexdigest()
        ):
            invalid_success = True
            continue
        eligible_docs[source.id] = doc

    # VERIFYING completion requires a Run-scoped receipt. The absence of
    # Evidence rows alone is allowed; the absence of an extraction receipt is
    # not. A receipt is only written when all scheduled model calls returned.
    problem = None
    manifest = await session.get(ResearchEvidenceManifestRow, run_id, with_for_update=True)
    if manifest is None:
        problem = "no run-scoped evidence completion manifest"
    elif manifest.task_id != task_id or manifest.fencing_token != fencing_token:
        problem = "manifest task/generation does not match this fenced attempt"
    elif (
        manifest.eligible_document_count < 1
        or manifest.scheduled_chunk_count < 1
        or manifest.completed_chunk_count != manifest.scheduled_chunk_count
        or min(manifest.verified_count, manifest.created_count, manifest.reused_count) < 0
        or manifest.created_count + manifest.reused_count > manifest.verified_count
    ):
        problem = "manifest workload/counters are inconsistent"
    elif not isinstance(manifest.document_hashes, dict) or (
        len(manifest.document_hashes) != manifest.eligible_document_count
    ):
        problem = "manifest document inventory is inconsistent"
    elif invalid_success:
        problem = "one or more retrieved source documents have invalid content hashes"
    elif set(manifest.document_hashes) != {str(source_id) for source_id in eligible_docs} or any(
        eligible_docs[source_id].content_hash != content_hash
        for source_id, content_hash in ((UUID(key), value) for key, value in manifest.document_hashes.items())
    ):
        problem = "manifest document inventory does not match the durable eligible documents"
    elif not isinstance(manifest.evidence_ids, list) or (len(set(manifest.evidence_ids)) != len(manifest.evidence_ids)):
        problem = "manifest evidence ID inventory is invalid"
    elif not isinstance(manifest.evidence_hashes, dict) or (
        set(manifest.evidence_hashes) != set(manifest.evidence_ids)
    ):
        problem = "evidence hash inventory differs from referenced evidence IDs"
    elif manifest.verified_count == 0 and manifest.evidence_ids:
        problem = "zero verified candidates cannot reference evidence IDs"
    elif manifest.verified_count > 0 and not manifest.evidence_ids:
        problem = "verified candidates require a persistent Evidence reference"
    elif manifest.evidence_ids:
        referenced = (
            await session.scalars(
                select(ResearchEvidenceRow)
                .where(
                    ResearchEvidenceRow.task_id == task_id,
                    ResearchEvidenceRow.id.in_([UUID(id_string) for id_string in manifest.evidence_ids]),
                )
                .with_for_update(read=True)
            )
        ).all()
        if len(referenced) != len(manifest.evidence_ids) or any(
            str(row.source_id) not in manifest.document_hashes
            or not row.content.strip()
            or manifest.evidence_hashes.get(str(row.id))
            != evidence_fingerprint(row.source_id, row.locator, row.content)
            for row in referenced
        ):
            problem = "referenced evidence is missing or no longer matches the persisted fingerprint"

    base["manifest_problem"] = problem
    ResearchStageCompletionValidator.validate(stage, StageArtifactSnapshot(**base))
    await validate_run_claim_receipt(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
    await validate_run_claim_verification_receipt(session, task_id=task_id, run_id=run_id, fencing_token=fencing_token)
