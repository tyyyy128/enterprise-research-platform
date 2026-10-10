"""7B: immutable artifact facts and stage-completion decisions.

These checks do not prove that external LLM calls were exactly-once.  For
PostgreSQL, collect the facts and run these checks in the SAME transaction as
the fenced Stage Journal COMPLETED update.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from enterprise_research.application.errors import ResearchStageCompletionError
from enterprise_research.domain import ResearchRunStatus


@dataclass(frozen=True, slots=True)
class EvidenceCompletionManifest:
    """Run/fencing-scoped receipt written only after all selected chunks return.

    Empty evidence_ids is legitimate when all chunks were processed but no
    candidate survived mechanical verification. It is NOT legitimate to
    report a zero-sized extraction workload as successful verification.
    """

    task_id: UUID
    run_id: UUID
    fencing_token: int
    eligible_document_count: int
    scheduled_chunk_count: int
    completed_chunk_count: int
    verified_count: int
    created_count: int
    reused_count: int
    document_hashes: tuple[tuple[UUID, str], ...]
    evidence_ids: tuple[UUID, ...]
    evidence_hashes: tuple[tuple[UUID, str], ...]

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise ValueError("Evidence manifest requires a positive fencing token.")
        if self.eligible_document_count < 1 or self.scheduled_chunk_count < 1:
            raise ValueError("Evidence manifest must describe a nonempty extraction workload.")
        if self.completed_chunk_count != self.scheduled_chunk_count:
            raise ValueError("Evidence manifest cannot certify unfinished chunks.")
        if min(self.verified_count, self.created_count, self.reused_count) < 0:
            raise ValueError("Evidence manifest counters cannot be negative.")
        if self.created_count + self.reused_count > self.verified_count:
            raise ValueError("Evidence manifest counters are inconsistent.")
        if not self.document_hashes or len({i for i, _ in self.document_hashes}) != len(self.document_hashes):
            raise ValueError("Evidence manifest must list unique source documents.")
        if any(
            len(hash_value) != 64 or any(c not in "0123456789abcdef" for c in hash_value)
            for _, hash_value in self.document_hashes
        ):
            raise ValueError("Evidence manifest requires SHA-256 document hashes.")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Evidence manifest evidence IDs must be unique.")
        if self.verified_count == 0 and self.evidence_ids:
            raise ValueError("Zero verified candidates cannot reference evidence IDs.")
        if self.verified_count > 0 and not self.evidence_ids:
            raise ValueError("Verified candidates require a persistent Evidence reference.")
        if set(self.evidence_ids) != {i for i, _ in self.evidence_hashes} or len(self.evidence_ids) != len(
            self.evidence_hashes
        ):
            raise ValueError("Evidence manifest hashes must cover exactly the referenced evidence IDs.")
        if any(
            len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) for _, digest in self.evidence_hashes
        ):
            raise ValueError("Evidence manifest requires SHA-256 evidence hashes.")


def evidence_fingerprint(source_id: UUID, locator: str | None, content: str) -> str:
    """Bind the persisted Evidence source, locator and exact content."""
    canonical = json.dumps([str(source_id), locator, content], ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StageArtifactSnapshot:
    """Minimal durable facts observed while holding the Run row lock."""

    plan_question_ids: frozenset[UUID]
    missing_search_questions: frozenset[UUID] = frozenset()
    web_source_count: int = 0
    missing_retrieval_sources: frozenset[UUID] = frozenset()
    successful_retrieval_count: int = 0
    manifest_problem: str | None = "no verified evidence manifest"


class ResearchStageCompletionValidator:
    """Fail-closed policy for persisted stage artifact receipts.

    Only the run repository can obtain a transactionally stable snapshot and
    write the Journal under its live Lease. Direct callers must not treat
    this decision alone as permission to commit a stage.
    """

    @staticmethod
    def validate(stage: ResearchRunStatus, facts: StageArtifactSnapshot) -> None:
        if stage not in (
            ResearchRunStatus.PLANNING,
            ResearchRunStatus.SEARCHING,
            ResearchRunStatus.EXTRACTING,
            ResearchRunStatus.VERIFYING,
        ):
            raise ResearchStageCompletionError(f"No durable completion contract exists for {stage.value.upper()}.")
        if not facts.plan_question_ids:
            raise ResearchStageCompletionError("PLANNING proof missing: no persisted run-scoped plan/questions.")
        if stage == ResearchRunStatus.PLANNING:
            return
        if facts.web_source_count < 1 or facts.missing_search_questions:
            missing = ", ".join(str(i) for i in sorted(facts.missing_search_questions, key=str))
            raise ResearchStageCompletionError(f"SEARCHING proof missing: run-scoped question coverage ({missing}).")
        if stage == ResearchRunStatus.SEARCHING:
            return
        if facts.missing_retrieval_sources or facts.successful_retrieval_count < 1:
            missing = ", ".join(str(i) for i in sorted(facts.missing_retrieval_sources, key=str))
            raise ResearchStageCompletionError(
                f"EXTRACTING proof missing: document outcomes/usable content ({missing})."
            )
        if stage == ResearchRunStatus.EXTRACTING:
            return
        if facts.manifest_problem is not None:
            raise ResearchStageCompletionError(f"VERIFYING proof missing: {facts.manifest_problem}.")
