"""Conservative, read-only reconciliation before explicit in-flight handoff.

The inspector never claims a lease or executes a stage.  A completed stage
journal is necessary but is NOT sufficient: the relevant persisted artifacts
must also agree.  Unknown outcomes stay paused rather than being replayed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from enterprise_research.application.document_repository import RetrievedDocumentRepository
from enterprise_research.application.graph_checkpoint import (
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointConflictError,
    ResearchGraphCheckpointReader,
)
from enterprise_research.application.plan_repository import ResearchPlanRepository
from enterprise_research.application.repository import ResearchRepository
from enterprise_research.application.run_service import ResearchRunApplicationService
from enterprise_research.application.stage_journal import ResearchStageOutcome
from enterprise_research.domain import ResearchRunStatus, RetrievalStatus, SourceType

STAGE_ORDER = (
    ResearchRunStatus.PLANNING,
    ResearchRunStatus.SEARCHING,
    ResearchRunStatus.EXTRACTING,
    ResearchRunStatus.VERIFYING,
    ResearchRunStatus.WRITING,
)


class ResearchResumeDisposition(StrEnum):
    """Results are advice, not permission to bypass the database lease."""

    PENDING = "pending_requires_normal_start"
    READY = "ready_for_explicit_claim"
    PAUSED = "paused_for_reconciliation"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class ResearchResumeDecision:
    task_id: UUID
    run_id: UUID
    status: ResearchRunStatus
    disposition: ResearchResumeDisposition
    next_stage: ResearchRunStatus | None
    reason: str
    proof_digest: str | None = None


class ResearchSafeResumeInspector:
    """Read stage audit history and task artifacts without making any writes.

    Only PLANNING, SEARCHING and EXTRACTING have a sufficiently checkable
    durable artifact contract here.  VERIFYING can validly emit zero evidence;
    WRITING has no durable report artifact yet.  Both are therefore paused.
    """

    def __init__(
        self,
        runs: ResearchRunApplicationService,
        research: ResearchRepository,
        plans: ResearchPlanRepository,
        documents: RetrievedDocumentRepository,
        *,
        graph_checkpoints: ResearchGraphCheckpointReader | None = None,
    ) -> None:
        self._runs = runs
        self._research = research
        self._plans = plans
        self._documents = documents
        self._graph_checkpoints = graph_checkpoints

    async def inspect(self, task_id: UUID, run_id: UUID) -> ResearchResumeDecision:
        run = await self._runs.get_run(task_id, run_id)

        def decision(
            disposition: ResearchResumeDisposition,
            reason: str,
            *,
            next_stage: ResearchRunStatus | None = None,
            artifacts: list[object] | None = None,
        ) -> ResearchResumeDecision:
            digest = None
            if artifacts is not None:
                canonical = json.dumps(artifacts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            return ResearchResumeDecision(task_id, run_id, run.status, disposition, next_stage, reason, digest)

        if run.is_terminal:
            return decision(ResearchResumeDisposition.TERMINAL, "Terminal runs cannot be resumed.")
        if run.status == ResearchRunStatus.PENDING:
            return decision(ResearchResumeDisposition.PENDING, "Pending runs use normal start(), not resume().")

        if run.status not in STAGE_ORDER:
            return decision(ResearchResumeDisposition.PAUSED, "Unknown in-flight research stage.")
        current_index = STAGE_ORDER.index(run.status)
        attempts = await self._runs.inspect_stage_attempts(task_id, run_id)
        proof: list[object] = []
        for stage in STAGE_ORDER[: current_index + 1]:
            entries = [attempt for attempt in attempts if attempt.stage == stage]
            if len(entries) != 1 or entries[0].outcome != ResearchStageOutcome.COMPLETED:
                return decision(
                    ResearchResumeDisposition.PAUSED,
                    f"Stage {stage.value} has missing, duplicate, failed or in-doubt attempts.",
                )
            proof.append(
                (
                    stage.value,
                    entries[0].fencing_token,
                    entries[0].started_at.isoformat(),
                    entries[0].finished_at.isoformat() if entries[0].finished_at else None,
                )
            )
            if self._graph_checkpoints is not None:
                binding = ResearchGraphCheckpointBinding(task_id, run_id, stage, entries[0].fencing_token)
                try:
                    reference = await self._graph_checkpoints.latest(binding)
                except ResearchGraphCheckpointConflictError:
                    return decision(
                        ResearchResumeDisposition.PAUSED, "Graph checkpoint identity conflicts with stage journal."
                    )
                if reference is not None:
                    if reference.binding != binding:
                        return decision(
                            ResearchResumeDisposition.PAUSED, "Graph checkpoint has a mismatched Run binding."
                        )
                    # Advisory fingerprint only: checkpoints do not prove business completion.
                    proof.append(("graph_checkpoint", stage.value, reference.checkpoint_id))
        if any(attempt.stage in STAGE_ORDER[current_index + 1 :] for attempt in attempts):
            return decision(ResearchResumeDisposition.PAUSED, "Journal contains a future-stage attempt.")

        # Every safe resume path depends on a run-scoped, durable plan.
        plan = await self._plans.get_for_run(run_id)
        if plan is None or plan.task_id != task_id or plan.run_id != run_id or not plan.questions:
            return decision(
                ResearchResumeDisposition.PAUSED,
                "Persisted run-scoped research plan is missing or invalid.",
            )
        proof.append(plan.model_dump(mode="json"))

        if run.status == ResearchRunStatus.PLANNING:
            return decision(
                ResearchResumeDisposition.READY,
                "Completed planning attempt and matching persisted plan were verified.",
                next_stage=ResearchRunStatus.SEARCHING,
                artifacts=proof,
            )

        aggregate = await self._research.get(task_id)
        if aggregate is None:
            return decision(ResearchResumeDisposition.PAUSED, "Research aggregate is missing.")
        web_sources = sorted(
            (source for source in aggregate.sources.values() if source.source_type == SourceType.WEB and source.url),
            key=lambda source: str(source.source_id),
        )
        if not web_sources:
            return decision(ResearchResumeDisposition.PAUSED, "No persisted web sources exist.")

        # Require a hit for each planned question belonging to *this run*, not
        # just a source from an unrelated run on the same shared Task.
        observed_questions: set[tuple[str, str]] = set()
        for source in web_sources:
            metadata = source.metadata.get("search")
            hits = metadata.get("hits") if isinstance(metadata, dict) else None
            if isinstance(hits, list):
                for hit in hits:
                    if isinstance(hit, dict) and hit.get("run_id") == str(run_id):
                        question_id = hit.get("question_id")
                        query = hit.get("query")
                        if isinstance(question_id, str) and isinstance(query, str):
                            observed_questions.add((question_id, query))
            proof.append(source.model_dump(mode="json"))
        for question in plan.questions:
            if (str(question.question_id), question.question) not in observed_questions:
                return decision(
                    ResearchResumeDisposition.PAUSED,
                    "Not every planned question has durable, run-scoped search provenance.",
                )

        if run.status == ResearchRunStatus.SEARCHING:
            return decision(
                ResearchResumeDisposition.READY,
                "Completed search journal and question-to-source provenance were verified.",
                next_stage=ResearchRunStatus.EXTRACTING,
                artifacts=proof,
            )

        # Retrieval iterates ALL web sources on the task, not only sources
        # discovered by this run. Reused successful documents are permitted.
        succeeded = 0
        for source in web_sources:
            document = await self._documents.get_for_source(source.source_id)
            if (
                document is None
                or document.source_id != source.source_id
                or str(document.requested_url) != str(source.url)
            ):
                return decision(ResearchResumeDisposition.PAUSED, "Some web sources lack matching retrieval outcomes.")
            if document.status == RetrievalStatus.SUCCEEDED:
                succeeded += 1
            elif document.status == RetrievalStatus.FAILED:
                if document.metadata.get("retrieval_run_id") != str(run_id):
                    return decision(ResearchResumeDisposition.PAUSED, "Failed retrieval lacks current-run provenance.")
            else:
                return decision(ResearchResumeDisposition.PAUSED, "Unknown retrieval outcome.")
            # Keep the proof bounded by document metadata and its validated
            # content hash rather than serializing large fetched bodies twice.
            proof.append(document.model_dump(mode="json", exclude={"content"}))
        if succeeded < 1:
            return decision(ResearchResumeDisposition.PAUSED, "No successful persisted retrieval can be reused.")

        if run.status == ResearchRunStatus.EXTRACTING:
            return decision(
                ResearchResumeDisposition.READY,
                "Completed retrieval journal and all matching document outcomes were verified.",
                next_stage=ResearchRunStatus.VERIFYING,
                artifacts=proof,
            )

        # Evidence has no per-run completion manifest and Writing has no
        # durable report. A completed journal alone cannot close those gaps.
        return decision(
            ResearchResumeDisposition.PAUSED,
            "Evidence/report completion cannot yet be proven from durable run-scoped artifacts.",
        )
