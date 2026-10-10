"""3F: unverified, provenance-bound claim proposals from a run's evidence.

A model may propose wording and references; it MUST NOT assign SUPPORTED.
The repository is responsible for comparing a fresh, fenced database snapshot
against the model input before inserting task-level Claims and the run receipt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid5

from enterprise_research.application.errors import ResearchClaimGenerationError
from enterprise_research.application.run_lease import ResearchRunLease


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimGenerationLimits:
    max_evidence: int = 12
    max_excerpt_chars: int = 1_800
    max_candidates: int = 12
    max_claim_chars: int = 600
    max_references_per_claim: int = 5

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (
                self.max_evidence,
                self.max_excerpt_chars,
                self.max_candidates,
                self.max_claim_chars,
                self.max_references_per_claim,
            )
        ):
            raise ValueError("Claim generation limits must be positive integers.")
        if self.max_references_per_claim > self.max_evidence:
            raise ValueError("max_references_per_claim cannot exceed max_evidence.")


@dataclass(frozen=True, slots=True)
class ClaimInputEvidence:
    evidence_id: UUID
    source_id: UUID
    excerpt: str
    locator: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ClaimGenerationContext:
    task_id: UUID
    run_id: UUID
    fencing_token: int
    task_query: str
    objective: str
    questions: tuple[str, ...]
    evidence: tuple[ClaimInputEvidence, ...]
    available_evidence_count: int
    input_sha256: str


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    """Unverified model output: text plus explicit evidence identifiers."""

    text: str
    evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ClaimGenerationReceipt:
    task_id: UUID
    run_id: UUID
    fencing_token: int
    input_sha256: str
    output_sha256: str
    available_evidence_count: int
    selected_evidence_ids: tuple[UUID, ...]
    claim_ids: tuple[UUID, ...]
    generator_model: str


class ResearchClaimGenerator(Protocol):
    async def propose(self, context: ClaimGenerationContext) -> list[ClaimCandidate]:
        """Propose candidates only; never assess their support status."""


class ResearchClaimGenerationRepository(Protocol):
    async def load_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease, limits: ClaimGenerationLimits
    ) -> ClaimGenerationContext:
        """Read verified Run evidence under a fenced lease."""

    async def save_guarded(
        self,
        context: ClaimGenerationContext,
        lease: ResearchRunLease,
        limits: ClaimGenerationLimits,
        candidates: tuple[ClaimCandidate, ...],
        *,
        model: str,
    ) -> ClaimGenerationReceipt:
        """Revalidate the input and persist all candidates plus receipt atomically."""


class ResearchClaimGenerationService:
    """One bounded generation attempt after the VERIFYING evidence receipt."""

    def __init__(
        self,
        repository: ResearchClaimGenerationRepository,
        generator: ResearchClaimGenerator,
        *,
        model: str,
        limits: ClaimGenerationLimits = ClaimGenerationLimits(),
    ) -> None:
        if not model.strip():
            raise ValueError("Claim generator model identifier is required.")
        self._repository = repository
        self._generator = generator
        self._model = model
        self._limits = limits

    async def generate_run(
        self, task_id: UUID, run_id: UUID, *, lease: ResearchRunLease | None
    ) -> ClaimGenerationReceipt:
        if lease is None or lease.run_id != run_id:
            raise ResearchClaimGenerationError("Claim generation requires the current Run's fenced lease.")
        context = await self._repository.load_context(task_id, run_id, lease, self._limits)
        proposed = await self._generator.propose(context) if context.evidence else []
        validated = validate_claim_candidates(context, proposed, self._limits)
        return await self._repository.save_guarded(context, lease, self._limits, validated, model=self._model)


def validate_claim_candidates(
    context: ClaimGenerationContext,
    proposed: list[ClaimCandidate] | tuple[ClaimCandidate, ...],
    limits: ClaimGenerationLimits,
) -> tuple[ClaimCandidate, ...]:
    """Mechanical scope/size checks; these do NOT certify semantic support."""
    if not isinstance(proposed, (list, tuple)) or len(proposed) > limits.max_candidates:
        raise ResearchClaimGenerationError("Claim generator returned an invalid candidate count.")
    allowed = {item.evidence_id for item in context.evidence}
    seen: set[tuple[str, tuple[UUID, ...]]] = set()
    validated: list[ClaimCandidate] = []
    for candidate in proposed:
        if not isinstance(candidate, ClaimCandidate):
            raise ResearchClaimGenerationError("Claim generator must return structured ClaimCandidate values.")
        if not isinstance(candidate.text, str):
            raise ResearchClaimGenerationError("Claim text must be a string.")
        text = " ".join(candidate.text.split())
        evidence_ids = candidate.evidence_ids
        if not text or len(text) > limits.max_claim_chars:
            raise ResearchClaimGenerationError("Claim text is blank or exceeds the allowed length.")
        if (
            not isinstance(evidence_ids, tuple)
            or not all(isinstance(evidence_id, UUID) for evidence_id in evidence_ids)
            or not 1 <= len(evidence_ids) <= limits.max_references_per_claim
            or len(set(evidence_ids)) != len(evidence_ids)
            or not set(evidence_ids).issubset(allowed)
        ):
            raise ResearchClaimGenerationError("Claim references unknown, duplicated or out-of-scope evidence.")
        key = (text.casefold(), tuple(sorted(evidence_ids, key=str)))
        if key in seen:
            raise ResearchClaimGenerationError("Claim generator returned duplicate candidates.")
        seen.add(key)
        validated.append(ClaimCandidate(text=text, evidence_ids=evidence_ids))
    return tuple(validated)


def candidate_id(run_id: UUID, candidate: ClaimCandidate) -> UUID:
    """Stable Run-specific ID; never collides across different executions."""
    return uuid5(run_id, sha256_json([candidate.text, sorted(str(item) for item in candidate.evidence_ids)]))


def candidate_output_digest(run_id: UUID, candidates: tuple[ClaimCandidate, ...]) -> str:
    return sha256_json(
        [
            [str(candidate_id(run_id, candidate)), candidate.text, [str(item) for item in candidate.evidence_ids]]
            for candidate in candidates
        ]
    )
