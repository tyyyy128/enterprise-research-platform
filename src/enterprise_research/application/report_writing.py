"""Run-scoped report rendering with 7C facts and 3H bounded model grouping.

The baseline report is deterministic; the 3H variant is deterministically
rendered from a model-selected outline, never from unverified factual prose.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID

from enterprise_research.application.errors import ResearchStageCompletionError
from enterprise_research.application.run_lease import ResearchRunLease

if TYPE_CHECKING:
    from enterprise_research.application.report_synthesis import (
        ReportSynthesisContext,
        ReportSynthesisPlan,
    )


@dataclass(frozen=True, slots=True)
class ReportEvidenceReference:
    evidence_id: UUID
    source_id: UUID
    source_url: str
    locator: str | None
    excerpt: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReportClaimReference:
    claim_id: UUID
    text: str
    evidence: tuple[ReportEvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class ReportDraft:
    title: str
    body: str
    citations: tuple[dict, ...]
    body_sha256: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class ResearchRunReportArtifact:
    """One immutable report receipt tied to a run and its writing generation."""

    task_id: UUID
    run_id: UUID
    fencing_token: int
    title: str
    body: str
    citations: tuple[dict, ...]
    body_sha256: str
    payload_sha256: str
    created_at: datetime
    synthesis_model: str | None = None
    synthesis_sha256: str | None = None


class ResearchRunReportRepository(Protocol):
    async def write_guarded(self, task_id: UUID, run_id: UUID, lease: ResearchRunLease) -> ResearchRunReportArtifact:
        """Fenced, write-once report creation using durable, supported claims."""

    async def get(self, task_id: UUID, run_id: UUID) -> ResearchRunReportArtifact | None:
        """Read the immutable report receipt for a task and Run."""


class ResearchReportWritingService:
    """Explicit writer facade; delegates transactional proof to its repository."""

    def __init__(self, repository: ResearchRunReportRepository) -> None:
        self._repository = repository

    async def write_run(self, task_id: UUID, run_id: UUID, *, lease: ResearchRunLease | None) -> None:
        if lease is None:
            raise ResearchStageCompletionError("WRITING requires a fenced ResearchRun lease.")
        await self._repository.write_guarded(task_id, run_id, lease)


def _plain(text: str) -> str:
    """Prevent untrusted claim text or excerpts from injecting Markdown/HTML."""
    escaped = html.escape(" ".join(text.split()), quote=True)
    for marker in ("\\", "`", "*", "_", "[", "]", "(", ")", "#", "!", "|"):
        escaped = escaped.replace(marker, "\\" + marker)
    return escaped


def _reference_url(url: str) -> str:
    """Encode Markdown URL delimiters; never treat source URLs as literal markup."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or len(url) > 2048
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ResearchStageCompletionError("WRITING requires bounded HTTP(S) source URLs.")
    return quote(url, safe=":/?&=%#@+;,$-_.!~*'")


def _canonical(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def report_digest(title: str, body: str, citations: tuple[dict, ...]) -> str:
    """Hash the entire receipt, not only the printable report body."""
    return hashlib.sha256(_canonical([title, body, citations]).encode("utf-8")).hexdigest()


def render_verified_report(title: str, claims: tuple[ReportClaimReference, ...]) -> ReportDraft:
    """Render a bounded evidence-backed digest; never invent unsupported claims."""
    if not title.strip() or not claims:
        raise ResearchStageCompletionError("WRITING requires a title and at least one supported run-scoped claim.")
    if len(claims) > 50 or len(title) > 1000:
        raise ResearchStageCompletionError("WRITING report exceeds the declared claim/title budget.")
    claim_ids: set[UUID] = set()
    lines = [
        f"# {_plain(title)}",
        "",
        "> Deterministic digest of existing supported claims; not an independently verified LLM analysis.",
        "",
        "## Supported findings",
        "",
    ]
    citations: list[dict] = []
    for i, claim in enumerate(claims, start=1):
        if claim.claim_id in claim_ids or not claim.text.strip() or not claim.evidence:
            raise ResearchStageCompletionError("WRITING found a duplicate, empty or unsupported claim.")
        claim_ids.add(claim.claim_id)
        if len(claim.evidence) > 10:
            raise ResearchStageCompletionError("WRITING claim exceeds the evidence reference budget.")
        lines.append(f"{i}. {_plain(claim.text)}")
        refs: list[dict] = []
        seen: set[UUID] = set()
        for evidence in claim.evidence:
            if evidence.evidence_id in seen or not evidence.source_url.startswith(("https://", "http://")):
                raise ResearchStageCompletionError("WRITING requires unique, linkable evidence references.")
            seen.add(evidence.evidence_id)
            if len(evidence.fingerprint) != 64:
                raise ResearchStageCompletionError("WRITING requires a SHA-256 evidence fingerprint.")
            ref = {
                "evidence_id": str(evidence.evidence_id),
                "source_id": str(evidence.source_id),
                "url": evidence.source_url,
                "locator": evidence.locator,
                "fingerprint": evidence.fingerprint,
            }
            refs.append(ref)
            location = f" · {_plain(evidence.locator)}" if evidence.locator else ""
            lines.append(
                f"   - Evidence {_plain(str(evidence.evidence_id))}: "
                f"{_plain(evidence.excerpt[:500])} — "
                f"[Source {_plain(str(evidence.source_id))}]({_reference_url(evidence.source_url)}){location}"
            )
        citations.append({"claim_id": str(claim.claim_id), "evidence": refs})
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > 200_000:
        raise ResearchStageCompletionError("WRITING report body exceeds the declared size budget.")
    output = tuple(citations)
    return ReportDraft(
        title=title,
        body=body,
        citations=output,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        payload_sha256=report_digest(title, body, output),
    )


def render_synthesized_report(context: ReportSynthesisContext, plan: ReportSynthesisPlan) -> ReportDraft:
    """Render model-selected grouping, never generated factual prose or URLs."""
    from enterprise_research.application.report_synthesis import validate_synthesis_plan

    validate_synthesis_plan(context, plan)
    claims = {claim.claim_id: claim for claim in context.claims}
    citation_catalog = render_verified_report(context.title, context.claims)
    by_id = {item["claim_id"]: item for item in citation_catalog.citations}
    lines = [
        f"# {_plain(context.title)}",
        "",
        "> Model-guided outline over independently verified claims; grouping is not a new factual finding.",
        "",
    ]
    if plan.highlights:
        lines.extend(["## Executive overview", ""])
        for claim_id in plan.highlights:
            lines.append(f"- {_plain(claims[claim_id].text)} (Claim `{claim_id}`)")
        lines.append("")
    citations = []
    for section in plan.sections:
        lines.extend([f"## {section.heading}", ""])
        if len(section.claim_ids) > 1:
            lines.extend(
                [
                    "The statements below are grouped for comparison; no causal relationship is implied.",
                    "",
                ]
            )
        for position, claim_id in enumerate(section.claim_ids, 1):
            claim = claims[claim_id]
            lines.append(f"{position}. {_plain(claim.text)} (Claim `{claim_id}`)")
            citation = by_id[str(claim_id)]
            citations.append(citation)
            for evidence, reference in zip(claim.evidence, citation["evidence"], strict=True):
                location = f" · {_plain(evidence.locator)}" if evidence.locator else ""
                # Use the exact already-validated source information; never a model-provided URL.
                lines.append(
                    f"   - Evidence {_plain(str(evidence.evidence_id))}: "
                    f"{_plain(evidence.excerpt[:500])} — "
                    f"[Source {_plain(str(evidence.source_id))}]({_reference_url(reference['url'])}){location}"
                )
            lines.append("")
    if context.caveats:
        lines.extend(
            [
                "## Conflicts and evidence limitations",
                "",
                "The following candidate claims did not qualify as supported findings:",
                "",
            ]
        )
        for caveat in context.caveats:
            label = "Conflicting evidence" if caveat.status == "conflicted" else "Insufficient support"
            lines.append(
                f"- **{label}** (Claim `{caveat.claim_id}`): {_plain(caveat.text)} "
                f"— Reviewer rationale: {_plain(caveat.rationale)}"
            )
            for kind, quotes in (("Supporting excerpt", caveat.supporting), ("Contrary excerpt", caveat.contradicting)):
                for item in quotes:
                    lines.append(
                        f"  - {kind}: {_plain(item.quote)} "
                        f"— [Source]({_reference_url(item.source_url)}) (Evidence `{item.evidence_id}`)"
                    )
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > 200_000:
        raise ResearchStageCompletionError("WRITING synthesized report body exceeds the declared size budget.")
    refs = tuple(citations)
    return ReportDraft(
        title=context.title,
        body=body,
        citations=refs,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        payload_sha256=report_digest(context.title, body, refs),
    )
