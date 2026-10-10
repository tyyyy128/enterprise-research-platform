"""Application service for mechanically verified evidence extraction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from enterprise_research.application.document_chunking import (
    DocumentChunkingSettings,
    chunk_document,
)
from enterprise_research.application.document_repository import (
    RetrievedDocumentRepository,
)
from enterprise_research.application.errors import (
    ResearchEvidenceExtractionError,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.evidence_extraction import (
    EvidenceExtractor,
)
from enterprise_research.application.evidence_manifest_repository import ResearchEvidenceManifestRepository
from enterprise_research.application.evidence_progress import (
    EvidenceExtractionProgress,
    EvidenceProgressCallback,
    emit_evidence_progress,
)
from enterprise_research.application.evidence_verification import (
    verify_evidence_candidate,
)
from enterprise_research.application.evidence_workload import (
    EvidenceDocumentChunks,
    EvidenceWorkloadSettings,
    plan_evidence_workload,
)
from enterprise_research.application.plan_repository import (
    ResearchPlanRepository,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.application.stage_completion import EvidenceCompletionManifest, evidence_fingerprint
from enterprise_research.domain import (
    Evidence,
    ResearchRunStatus,
    RetrievalStatus,
)


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceExtractionSummary:
    """Deterministic outcome counters for one evidence extraction stage."""

    eligible_document_count: int
    selected_document_count: int
    available_chunk_count: int
    scheduled_chunk_count: int
    skipped_chunk_count: int
    candidate_count: int
    verified_count: int
    rejected_count: int
    deduplicated_count: int
    created_count: int
    reused_count: int


class ResearchEvidenceExtractionService:
    """Extract and persist mechanically verified research evidence."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
        plan_repository: ResearchPlanRepository,
        document_repository: RetrievedDocumentRepository,
        extractor: EvidenceExtractor,
        *,
        chunking_settings: DocumentChunkingSettings | None = None,
        workload_settings: EvidenceWorkloadSettings | None = None,
        max_concurrency: int = 4,
        manifest_repository: ResearchEvidenceManifestRepository | None = None,
    ) -> None:
        self._research_repository = research_repository
        self._run_repository = run_repository
        self._plan_repository = plan_repository
        self._document_repository = document_repository
        self._extractor = extractor
        self._chunking_settings = chunking_settings or DocumentChunkingSettings()
        self._workload_settings = workload_settings or EvidenceWorkloadSettings()
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1.")

        self._max_concurrency = max_concurrency
        self._manifest_repository = manifest_repository

    async def extract_run(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        progress_callback: EvidenceProgressCallback | None = None,
        lease: ResearchRunLease | None = None,
    ) -> EvidenceExtractionSummary:
        """Extract verified evidence and report completed chunks in real time."""
        aggregate = await self._research_repository.get(task_id)
        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = await self._run_repository.get(run_id)
        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")
        if run.status != ResearchRunStatus.VERIFYING:
            raise ResearchEvidenceExtractionError(
                "Research run must be in 'verifying' state before evidence extraction."
            )

        plan = await self._plan_repository.get_for_run(run_id)
        if plan is None or plan.task_id != task_id:
            raise ResearchEvidenceExtractionError("Research run has no valid persisted research plan.")

        existing_keys = {
            (evidence.source_id, evidence.locator, evidence.content) for evidence in aggregate.evidence.values()
        }
        source_by_id = {source.source_id: source for source in aggregate.sources.values()}

        # Phase 1: load and chunk documents without invoking an LLM.
        document_chunks: list[EvidenceDocumentChunks] = []
        eligible_document_hashes: dict[UUID, str] = {}
        for source in aggregate.sources.values():
            document = await self._document_repository.get_for_source(source.source_id)
            if document is None:
                continue
            if document.source_id != source.source_id:
                raise ResearchEvidenceExtractionError(
                    "Retrieved document source does not match the requested research source."
                )
            if document.status != RetrievalStatus.SUCCEEDED or document.content is None:
                continue

            chunks = chunk_document(document.content, self._chunking_settings)
            document_chunks.append(EvidenceDocumentChunks(source_id=source.source_id, chunks=tuple(chunks)))
            if chunks and document.content_hash is not None:
                eligible_document_hashes[source.source_id] = document.content_hash

        # Phase 2: enforce workload limits before making model calls.
        workload = plan_evidence_workload(document_chunks, self._workload_settings)
        if workload.eligible_document_count == 0 or (
            self._manifest_repository is not None and workload.scheduled_chunk_count == 0
        ):
            raise ResearchEvidenceExtractionError(
                "Research run has no successfully retrieved documents eligible for evidence extraction."
            )

        completed_chunk_count = 0
        candidate_count = 0
        verified_count = 0
        rejected_count = 0
        deduplicated_count = 0
        created_count = 0
        reused_count = 0
        verified_keys: set[tuple[UUID, str, str]] = set()

        def snapshot() -> EvidenceExtractionProgress:
            return EvidenceExtractionProgress(
                task_id=task_id,
                run_id=run_id,
                eligible_document_count=workload.eligible_document_count,
                selected_document_count=workload.selected_document_count,
                available_chunk_count=workload.available_chunk_count,
                scheduled_chunk_count=workload.scheduled_chunk_count,
                skipped_chunk_count=workload.skipped_chunk_count,
                completed_chunk_count=completed_chunk_count,
                candidate_count=candidate_count,
                verified_count=verified_count,
                rejected_count=rejected_count,
                deduplicated_count=deduplicated_count,
                created_count=created_count,
                reused_count=reused_count,
            )

        # Fail fast on a broken progress sink, before starting background work.
        # For an empty schedule, 0/0 is the only (complete) progress snapshot.
        await emit_evidence_progress(progress_callback, snapshot())

        # Phase 3: extraction is concurrent; bookkeeping remains sequential.
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def extract_item(work_index, item):
            source = source_by_id.get(item.source_id)
            if source is None:
                raise ResearchEvidenceExtractionError("Evidence workload referenced an unknown research source.")
            async with semaphore:
                candidates = await self._extractor.extract(
                    task=aggregate.task,
                    run=run,
                    plan=plan,
                    source=source,
                    chunk=item.chunk,
                )
            return work_index, item, source, candidates

        extraction_tasks = [asyncio.create_task(extract_item(index, item)) for index, item in enumerate(workload.items)]

        # Stage verified evidence, but do not mutate the aggregate while model
        # calls or progress callbacks can still fail.
        # Keep the earliest workload/candidate occurrence for deterministic
        # deduplication even when later chunks finish first.
        staged: dict[tuple[UUID, str, str], tuple[int, int, Evidence]] = {}

        try:
            # Unlike gather(), as_completed() lets us notify after each result.
            for completed in asyncio.as_completed(extraction_tasks):
                work_index, item, source, candidates = await completed
                candidate_count += len(candidates)

                for candidate_index, candidate in enumerate(candidates):
                    verified = verify_evidence_candidate(candidate, item.chunk)
                    if verified is None:
                        rejected_count += 1
                        continue

                    verified_count += 1
                    locator = f"chars:{verified.start_offset}-{verified.end_offset}"
                    key = (source.source_id, locator, verified.content)
                    verified_keys.add(key)

                    if key in existing_keys:
                        reused_count += 1
                        continue

                    occurrence = (work_index, candidate_index)
                    if key in staged:
                        deduplicated_count += 1
                        previous = staged[key]
                        if occurrence < previous[:2]:
                            staged[key] = (
                                work_index,
                                candidate_index,
                                Evidence(
                                    source_id=source.source_id,
                                    content=verified.content,
                                    locator=locator,
                                    relevance_score=verified.relevance_score,
                                ),
                            )
                        continue

                    staged[key] = (
                        work_index,
                        candidate_index,
                        Evidence(
                            source_id=source.source_id,
                            content=verified.content,
                            locator=locator,
                            relevance_score=verified.relevance_score,
                        ),
                    )
                    created_count += 1

                completed_chunk_count += 1
                await emit_evidence_progress(progress_callback, snapshot())

        except BaseException:
            # Includes cancellation of extract_run and callback errors.
            # as_completed() does not cancel outstanding tasks by itself.
            for task in extraction_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*extraction_tasks, return_exceptions=True)
            raise

        # Phase 4: apply evidence in the planner's order and persist once.
        # None of the asynchronous extraction/callback paths above can leave
        # partially added evidence in a shared in-memory aggregate.
        for _, _, evidence in sorted(staged.values(), key=lambda entry: entry[:2]):
            aggregate.add_evidence(evidence)

        if created_count > 0:
            if lease is None:
                await self._research_repository.save(aggregate)
            else:
                await self._research_repository.save_guarded(
                    aggregate,
                    lease,
                    ResearchRunStatus.VERIFYING,
                    run_repository=self._run_repository,
                )

        # Emit the fenced, durable receipt after ALL scheduled calls and
        # the evidence aggregate write have completed. No receipt is emitted
        # if a chunk fails or the progress callback raises.
        if self._manifest_repository is not None:
            if lease is None:
                raise ResearchEvidenceExtractionError("Evidence completion receipt requires a fenced lease.")
            referenced_ids = tuple(
                sorted(
                    (
                        item.evidence_id
                        for item in aggregate.evidence.values()
                        if (item.source_id, item.locator, item.content) in verified_keys
                    ),
                    key=str,
                )
            )
            manifest = EvidenceCompletionManifest(
                task_id=task_id,
                run_id=run_id,
                fencing_token=lease.fencing_token,
                eligible_document_count=workload.eligible_document_count,
                scheduled_chunk_count=workload.scheduled_chunk_count,
                completed_chunk_count=completed_chunk_count,
                verified_count=verified_count,
                created_count=created_count,
                reused_count=reused_count,
                document_hashes=tuple(sorted(eligible_document_hashes.items(), key=lambda item: str(item[0]))),
                evidence_ids=referenced_ids,
                evidence_hashes=tuple(
                    sorted(
                        (
                            (
                                evidence.evidence_id,
                                evidence_fingerprint(evidence.source_id, evidence.locator, evidence.content),
                            )
                            for evidence in aggregate.evidence.values()
                            if evidence.evidence_id in referenced_ids
                        ),
                        key=lambda item: str(item[0]),
                    )
                ),
            )
            await self._manifest_repository.save_guarded(manifest, lease)

        return EvidenceExtractionSummary(
            eligible_document_count=workload.eligible_document_count,
            selected_document_count=workload.selected_document_count,
            available_chunk_count=workload.available_chunk_count,
            scheduled_chunk_count=workload.scheduled_chunk_count,
            skipped_chunk_count=workload.skipped_chunk_count,
            candidate_count=candidate_count,
            verified_count=verified_count,
            rejected_count=rejected_count,
            deduplicated_count=deduplicated_count,
            created_count=created_count,
            reused_count=reused_count,
        )
