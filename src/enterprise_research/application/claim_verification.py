"""3G: bounded, evidence-quoted semantic verdicts for 3F Claim proposals.

An LLM judgment is NOT mathematical truth: SUPPORTED means that the configured
verifier judged the text supported by cited, exact source excerpts. The
PostgreSQL repository independently enforces provenance, scope, integrity and
fencing before promoting the task-level Claim's status.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from enterprise_research.application.claim_generation import ClaimInputEvidence, sha256_json
from enterprise_research.application.errors import ResearchClaimVerificationError
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.domain import ClaimStatus


@dataclass(frozen=True, slots=True)
class ClaimVerificationLimits:
    max_claims: int = 12
    max_evidence: int = 12
    max_excerpt_chars: int = 1800
    max_quote_chars: int = 320
    max_rationale_chars: int = 1000
    min_supported_confidence: float = 0.8

    def __post_init__(self) -> None:
        for value in (
            self.max_claims,
            self.max_evidence,
            self.max_excerpt_chars,
            self.max_quote_chars,
            self.max_rationale_chars,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("Claim verification budgets must be positive integers.")
        if (
            isinstance(self.min_supported_confidence, bool)
            or not isinstance(self.min_supported_confidence, (int, float))
            or not math.isfinite(self.min_supported_confidence)
            or not 0 < self.min_supported_confidence <= 1
        ):
            raise ValueError("The minimum supported confidence must be between 0 and 1.")


@dataclass(frozen=True, slots=True)
class VerificationClaim:
    claim_id: UUID
    text: str
    evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class VerificationContext:
    task_id: UUID
    run_id: UUID
    fencing_token: int
    generation_input_sha256: str
    generation_output_sha256: str
    evidence: tuple[ClaimInputEvidence, ...]
    claims: tuple[VerificationClaim, ...]
    input_sha256: str


@dataclass(frozen=True, slots=True)
class EvidenceQuote:
    evidence_id: UUID
    quote: str


@dataclass(frozen=True, slots=True)
class ClaimVerificationVerdict:
    claim_id: UUID
    status: ClaimStatus
    confidence: float
    rationale: str
    supporting: tuple[EvidenceQuote, ...] = ()
    contradicting: tuple[EvidenceQuote, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimVerificationReceipt:
    task_id: UUID
    run_id: UUID
    fencing_token: int
    input_sha256: str
    verdicts_sha256: str
    claim_ids: tuple[UUID, ...]
    verifier_model: str


class ResearchClaimVerifier(Protocol):
    async def assess(self, context: VerificationContext) -> list[ClaimVerificationVerdict]:
        """Assess each candidate; do not directly mutate Claims."""


class ResearchClaimVerificationRepository(Protocol):
    async def load_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease, limits: ClaimVerificationLimits
    ) -> VerificationContext: ...

    async def save_guarded(
        self,
        context: VerificationContext,
        lease: ResearchRunLease,
        limits: ClaimVerificationLimits,
        verdicts: tuple[ClaimVerificationVerdict, ...],
        *,
        model: str,
    ) -> ClaimVerificationReceipt: ...


class ResearchClaimVerificationService:
    """Verify all 3F candidates; zero candidates produce an explicit receipt."""

    def __init__(
        self,
        repository: ResearchClaimVerificationRepository,
        verifier: ResearchClaimVerifier,
        *,
        model: str,
        limits: ClaimVerificationLimits = ClaimVerificationLimits(),
    ) -> None:
        if not model.strip() or len(model) > 128:
            raise ValueError("A bounded, nonempty verifier model identifier is required.")
        self._repository, self._verifier, self._model, self._limits = repository, verifier, model, limits

    async def verify_run(
        self, task_id: UUID, run_id: UUID, *, lease: ResearchRunLease | None
    ) -> ClaimVerificationReceipt:
        if lease is None or lease.run_id != run_id:
            raise ResearchClaimVerificationError("Claim verification requires the current Run's fenced lease.")
        context = await self._repository.load_context(task_id, run_id, lease, self._limits)
        proposed = await self._verifier.assess(context) if context.claims else []
        verdicts = validate_verdicts(context, proposed, self._limits)
        return await self._repository.save_guarded(context, lease, self._limits, verdicts, model=self._model)


def _quotes(
    quotes: tuple[EvidenceQuote, ...], *, allowed: set[UUID], evidence: dict[UUID, ClaimInputEvidence], limit: int
) -> tuple[EvidenceQuote, ...]:
    if not isinstance(quotes, tuple) or len(quotes) > len(evidence):
        raise ResearchClaimVerificationError("Verification quote inventory is invalid.")
    output: list[EvidenceQuote] = []
    seen: set[tuple[UUID, str]] = set()
    for item in quotes:
        if not isinstance(item, EvidenceQuote) or not isinstance(item.evidence_id, UUID):
            raise ResearchClaimVerificationError("Verification citations must be structured evidence/quote pairs.")
        if not isinstance(item.quote, str):
            raise ResearchClaimVerificationError("Verification citation quote must be text.")
        quote = item.quote.strip()
        if (
            item.evidence_id not in allowed
            or item.evidence_id not in evidence
            or not quote
            or len(quote) > limit
            or quote not in evidence[item.evidence_id].excerpt
        ):
            raise ResearchClaimVerificationError("Verification citation is not an exact in-scope evidence quote.")
        key = (item.evidence_id, quote)
        if key in seen:
            raise ResearchClaimVerificationError("Verification citations must not be duplicated.")
        seen.add(key)
        output.append(EvidenceQuote(item.evidence_id, quote))
    return tuple(output)


def validate_verdicts(
    context: VerificationContext,
    proposed: list[ClaimVerificationVerdict] | tuple[ClaimVerificationVerdict, ...],
    limits: ClaimVerificationLimits,
) -> tuple[ClaimVerificationVerdict, ...]:
    """Fail closed on absent, surplus or unquoted model results."""
    if not isinstance(proposed, (tuple, list)) or len(proposed) != len(context.claims):
        raise ResearchClaimVerificationError("Verifier must decide exactly once for every generated Claim.")
    claims = {claim.claim_id: claim for claim in context.claims}
    evidence = {item.evidence_id: item for item in context.evidence}
    seen: set[UUID] = set()
    validated: dict[UUID, ClaimVerificationVerdict] = {}
    for verdict in proposed:
        if not isinstance(verdict, ClaimVerificationVerdict) or verdict.claim_id not in claims:
            raise ResearchClaimVerificationError("Verifier returned an unknown or malformed Claim decision.")
        if verdict.claim_id in seen:
            raise ResearchClaimVerificationError("Verifier returned duplicate Claim decisions.")
        seen.add(verdict.claim_id)
        if not isinstance(verdict.status, ClaimStatus):
            raise ResearchClaimVerificationError("Verifier must return a valid semantic Claim status.")
        if (
            not isinstance(verdict.confidence, (float, int))
            or isinstance(verdict.confidence, bool)
            or not math.isfinite(verdict.confidence)
            or not 0 <= verdict.confidence <= 1
        ):
            raise ResearchClaimVerificationError("Verifier confidence must be finite and between 0 and 1.")
        if not isinstance(verdict.rationale, str):
            raise ResearchClaimVerificationError("Verifier rationale must be text.")
        rationale = verdict.rationale.strip()
        if not rationale or len(rationale) > limits.max_rationale_chars:
            raise ResearchClaimVerificationError("Verifier rationale is missing or over budget.")
        referenced = set(claims[verdict.claim_id].evidence_ids)
        supporting = _quotes(verdict.supporting, allowed=referenced, evidence=evidence, limit=limits.max_quote_chars)
        contradicting = _quotes(
            verdict.contradicting, allowed=set(evidence), evidence=evidence, limit=limits.max_quote_chars
        )
        if verdict.status == ClaimStatus.SUPPORTED:
            if not supporting or contradicting or verdict.confidence < limits.min_supported_confidence:
                raise ResearchClaimVerificationError(
                    "SUPPORTED requires direct quotes, no conflicts and high confidence."
                )
        elif verdict.status == ClaimStatus.CONFLICTED:
            if not supporting or not contradicting or set(supporting) & set(contradicting):
                raise ResearchClaimVerificationError("CONFLICTED requires distinct supporting and contrary quotes.")
        elif supporting:
            raise ResearchClaimVerificationError("UNSUPPORTED must not include claimed supporting quotations.")
        validated[verdict.claim_id] = ClaimVerificationVerdict(
            verdict.claim_id,
            verdict.status,
            round(float(verdict.confidence), 4),
            rationale,
            supporting,
            contradicting,
        )
    return tuple(validated[item.claim_id] for item in context.claims)


def serialize_verdict(verdict: ClaimVerificationVerdict) -> dict[str, object]:
    return {
        "claim_id": str(verdict.claim_id),
        "status": verdict.status.value,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "supporting": [[str(q.evidence_id), q.quote] for q in verdict.supporting],
        "contradicting": [[str(q.evidence_id), q.quote] for q in verdict.contradicting],
    }


def verdicts_digest(verdicts: tuple[ClaimVerificationVerdict, ...]) -> str:
    return sha256_json([serialize_verdict(item) for item in verdicts])


def verification_receipt_digest(
    verdicts: tuple[ClaimVerificationVerdict, ...], *, model: str, limits: ClaimVerificationLimits
) -> str:
    """Bind the verifier identity and limits as well as its decisions."""
    return sha256_json(
        {
            "verifier_model": model,
            "limits": {
                "max_claims": limits.max_claims,
                "max_evidence": limits.max_evidence,
                "max_excerpt_chars": limits.max_excerpt_chars,
                "max_quote_chars": limits.max_quote_chars,
                "max_rationale_chars": limits.max_rationale_chars,
                "min_supported_confidence": limits.min_supported_confidence,
            },
            "verdicts": [serialize_verdict(item) for item in verdicts],
        }
    )
