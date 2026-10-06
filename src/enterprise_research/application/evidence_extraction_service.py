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
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
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

    async def extract_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> EvidenceExtractionSummary:
        """Extract verified evidence for one research run."""
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
            (
                evidence.source_id,
                evidence.locator,
                evidence.content,
            )
            for evidence in aggregate.evidence.values()
        }

        seen_keys: set[
            tuple[
                UUID,
                str,
                str,
            ]
        ] = set()

        source_by_id = {source.source_id: source for source in aggregate.sources.values()}

        document_chunks: list[EvidenceDocumentChunks] = []

        #
        # Phase 1:
        # 先只读取成功抓取的正文并做 deterministic chunking。
        #
        # 注意：
        # 这一阶段绝不能调用 LLM。
        #
        for source in aggregate.sources.values():
            document = await self._document_repository.get_for_source(source.source_id)

            if document is None:
                continue

            if document.source_id != source.source_id:
                raise ResearchEvidenceExtractionError(
                    "Retrieved document source does not match the requested research source."
                )

            if document.status != RetrievalStatus.SUCCEEDED:
                continue

            if document.content is None:
                continue

            chunks = chunk_document(
                document.content,
                self._chunking_settings,
            )

            document_chunks.append(
                EvidenceDocumentChunks(
                    source_id=(source.source_id),
                    chunks=tuple(chunks),
                )
            )

        #
        # Phase 2:
        # 在任何 Evidence LLM 调用之前做 workload planning。
        #
        # 默认硬上限：
        #
        #   max_documents = 4
        #   max_chunks_per_document = 8
        #   max_total_chunks = 24
        #
        workload = plan_evidence_workload(
            document_chunks,
            self._workload_settings,
        )

        if workload.eligible_document_count == 0:
            raise ResearchEvidenceExtractionError(
                "Research run has no successfully retrieved documents eligible for evidence extraction."
            )

        candidate_count = 0
        verified_count = 0
        rejected_count = 0
        deduplicated_count = 0
        created_count = 0
        reused_count = 0

        #
        # Phase 3:
        # 只有 workload planner 选中的 chunk
        # 才有资格调用 Evidence LLM。
        #
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def extract_item(item):
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

            return item, source, candidates

        extraction_tasks = [asyncio.create_task(extract_item(item)) for item in workload.items]

        try:
            extraction_results = await asyncio.gather(*extraction_tasks)

        except BaseException:
            for task in extraction_tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                *extraction_tasks,
                return_exceptions=True,
            )

            raise

        for item, source, candidates in extraction_results:
            candidate_count += len(candidates)

            for candidate in candidates:
                verified = verify_evidence_candidate(
                    candidate,
                    item.chunk,
                )

                if verified is None:
                    rejected_count += 1
                    continue

                verified_count += 1

                locator = f"chars:{verified.start_offset}-{verified.end_offset}"

                key = (
                    source.source_id,
                    locator,
                    verified.content,
                )

                if key in existing_keys:
                    reused_count += 1
                    continue

                if key in seen_keys:
                    deduplicated_count += 1
                    continue

                seen_keys.add(key)

                aggregate.add_evidence(
                    Evidence(
                        source_id=(source.source_id),
                        content=(verified.content),
                        locator=locator,
                        relevance_score=(verified.relevance_score),
                    )
                )

                created_count += 1

        if created_count > 0:
            await self._research_repository.save(aggregate)

        return EvidenceExtractionSummary(
            eligible_document_count=(workload.eligible_document_count),
            selected_document_count=(workload.selected_document_count),
            available_chunk_count=(workload.available_chunk_count),
            scheduled_chunk_count=(workload.scheduled_chunk_count),
            skipped_chunk_count=(workload.skipped_chunk_count),
            candidate_count=(candidate_count),
            verified_count=(verified_count),
            rejected_count=(rejected_count),
            deduplicated_count=(deduplicated_count),
            created_count=(created_count),
            reused_count=(reused_count),
        )
