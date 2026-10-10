"""3H: bounded model-selected report structure over run-verified facts.

The model chooses which already-verified Claims to highlight and group. It does
not author free-form factual paragraphs: rendering uses the original Claim text
and linked Evidence verbatim, and always surfaces adverse 3G verdicts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from enterprise_research.application.errors import ResearchReportWritingError
from enterprise_research.application.report_writing import ReportClaimReference
from enterprise_research.application.run_lease import ResearchRunLease

SECTION_HEADINGS = (
    "Key findings",
    "Related findings",
    "Additional findings",
    "Other verified findings",
)


@dataclass(frozen=True, slots=True)
class ReportSynthesisQuote:
    evidence_id: UUID
    quote: str
    source_url: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReportSynthesisCaveat:
    claim_id: UUID
    text: str
    status: str
    rationale: str
    supporting: tuple[ReportSynthesisQuote, ...] = ()
    contradicting: tuple[ReportSynthesisQuote, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportSynthesisSection:
    heading: str
    claim_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ReportSynthesisPlan:
    highlights: tuple[UUID, ...]
    sections: tuple[ReportSynthesisSection, ...]


@dataclass(frozen=True, slots=True)
class ReportSynthesisContext:
    task_id: UUID
    run_id: UUID
    fencing_token: int
    title: str
    claims: tuple[ReportClaimReference, ...]
    caveats: tuple[ReportSynthesisCaveat, ...]
    input_sha256: str


def plan_data(plan: ReportSynthesisPlan) -> dict[str, object]:
    return {
        "highlights": [str(item) for item in plan.highlights],
        "sections": [
            {"heading": section.heading, "claim_ids": [str(item) for item in section.claim_ids]}
            for section in plan.sections
        ],
    }


def plan_from_data(data: object) -> ReportSynthesisPlan:
    """Strictly deserialize a persisted model plan; reject extra keys and invalid IDs."""
    try:
        if not isinstance(data, dict) or set(data) != {"highlights", "sections"}:
            raise ValueError("malformed synthesis record")
        highlights = data["highlights"]
        sections = data["sections"]
        if not isinstance(highlights, list) or not isinstance(sections, list):
            raise ValueError("malformed synthesis collections")
        output = []
        for section in sections:
            if not isinstance(section, dict) or set(section) != {"heading", "claim_ids"}:
                raise ValueError("malformed synthesis section")
            if not isinstance(section["heading"], str) or not isinstance(section["claim_ids"], list):
                raise ValueError("malformed synthesis fields")
            output.append(
                ReportSynthesisSection(
                    section["heading"],
                    tuple(UUID(item) for item in section["claim_ids"] if isinstance(item, str)),
                )
            )
            if len(output[-1].claim_ids) != len(section["claim_ids"]):
                raise ValueError("malformed synthesis claim identifiers")
        if any(not isinstance(item, str) for item in highlights):
            raise ValueError("malformed highlights")
        return ReportSynthesisPlan(tuple(UUID(item) for item in highlights), tuple(output))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ResearchReportWritingError("WRITING synthesis plan record is malformed.") from exc


def validate_synthesis_plan(context: ReportSynthesisContext, proposal: ReportSynthesisPlan) -> ReportSynthesisPlan:
    if not isinstance(proposal, ReportSynthesisPlan) or not proposal.sections or len(proposal.sections) > 4:
        raise ResearchReportWritingError("WRITING synthesis must provide between one and four sections.")
    allowed = {claim.claim_id for claim in context.claims}
    if not allowed or len(allowed) != len(context.claims):
        raise ResearchReportWritingError("WRITING synthesis needs distinct supported Claims.")
    if len(proposal.highlights) > 3 or len(set(proposal.highlights)) != len(proposal.highlights):
        raise ResearchReportWritingError("WRITING synthesis highlights exceed the budget or are repeated.")
    if any(not isinstance(item, UUID) or item not in allowed for item in proposal.highlights):
        raise ResearchReportWritingError("WRITING synthesis highlights reference foreign or unknown Claims.")
    visited: set[UUID] = set()
    headings: set[str] = set()
    for section in proposal.sections:
        if not isinstance(section, ReportSynthesisSection) or section.heading not in SECTION_HEADINGS:
            raise ResearchReportWritingError("WRITING synthesis supplied an unapproved factual heading.")
        if section.heading in headings or not section.claim_ids:
            raise ResearchReportWritingError("WRITING synthesis headings must be distinct and nonempty.")
        headings.add(section.heading)
        for claim_id in section.claim_ids:
            if not isinstance(claim_id, UUID) or claim_id not in allowed or claim_id in visited:
                raise ResearchReportWritingError("WRITING synthesis omitted, repeated or invented a Claim reference.")
            visited.add(claim_id)
    if visited != allowed:
        raise ResearchReportWritingError("WRITING synthesis must cover every supported Run Claim exactly once.")
    return proposal


def synthesis_input_sha256(
    title: str,
    claims: tuple[ReportClaimReference, ...],
    caveats: tuple[ReportSynthesisCaveat, ...],
) -> str:
    """Fingerprint every factual input including source/excerpt and verification caveats."""
    data = [
        title,
        [
            [
                str(claim.claim_id),
                claim.text,
                [
                    [
                        str(ref.evidence_id),
                        str(ref.source_id),
                        ref.source_url,
                        ref.locator,
                        ref.excerpt,
                        ref.fingerprint,
                    ]
                    for ref in claim.evidence
                ],
            ]
            for claim in claims
        ],
        [
            [
                str(c.claim_id),
                c.text,
                c.status,
                c.rationale,
                [[str(q.evidence_id), q.quote, q.source_url, q.fingerprint] for q in c.supporting],
                [[str(q.evidence_id), q.quote, q.source_url, q.fingerprint] for q in c.contradicting],
            ]
            for c in caveats
        ],
    ]
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def synthesis_receipt_sha256(model: str, input_sha256: str, plan: ReportSynthesisPlan) -> str:
    if not isinstance(model, str) or not 0 < len(model.strip()) <= 128:
        raise ResearchReportWritingError("WRITING synthesis requires a bounded model identifier.")
    return hashlib.sha256(
        json.dumps([model, input_sha256, plan_data(plan)], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ResearchReportSynthesisPlanner(Protocol):
    async def compose(self, context: ReportSynthesisContext) -> ReportSynthesisPlan: ...


class ResearchReportSynthesisRepository(Protocol):
    async def load_synthesis_context(
        self, task_id: UUID, run_id: UUID, lease: ResearchRunLease
    ) -> ReportSynthesisContext: ...

    async def write_synthesized_guarded(
        self,
        context: ReportSynthesisContext,
        plan: ReportSynthesisPlan,
        lease: ResearchRunLease,
        *,
        model: str,
    ) -> None: ...


class ResearchReportSynthesisService:
    """One model call outside DB transactions, followed by revalidation under a Run lock."""

    def __init__(
        self, repository: ResearchReportSynthesisRepository, planner: ResearchReportSynthesisPlanner, *, model: str
    ) -> None:
        if not model.strip() or len(model) > 128:
            raise ValueError("A bounded report synthesis model identifier is required.")
        self._repository, self._planner, self._model = repository, planner, model

    async def write_run(self, task_id: UUID, run_id: UUID, *, lease: ResearchRunLease | None) -> None:
        if lease is None or lease.run_id != run_id:
            raise ResearchReportWritingError("WRITING synthesis requires the current Run's fenced lease.")
        context = await self._repository.load_synthesis_context(task_id, run_id, lease)
        proposal = await self._planner.compose(context)
        plan = validate_synthesis_plan(context, proposal)
        await self._repository.write_synthesized_guarded(context, plan, lease, model=self._model)
