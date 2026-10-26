"""Deterministic workload planning for evidence extraction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
)
from enterprise_research.application.financial_evidence_scoring import (
    declared_report_edition_years,
    financial_precision_rank,
    is_preferred_source,
    target_financial_year,
)


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceWorkloadSettings:
    """Bound evidence extraction work for one research run."""

    max_documents: int = 4
    max_chunks_per_document: int = 8
    max_total_chunks: int = 24
    selection_mode: str = "uuid"
    focus_terms: tuple[str, ...] = ()
    preferred_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_documents < 1:
            raise ValueError("max_documents must be at least 1.")

        if self.max_chunks_per_document < 1:
            raise ValueError("max_chunks_per_document must be at least 1.")

        if self.max_total_chunks < 1:
            raise ValueError("max_total_chunks must be at least 1.")

        if self.selection_mode not in {"uuid", "focus", "financial_precision"}:
            raise ValueError("selection_mode must be 'uuid', 'focus', or 'financial_precision'.")
        if self.selection_mode in {"focus", "financial_precision"} and not self.focus_terms:
            raise ValueError("focused selection requires at least one focus term.")
        if self.selection_mode == "uuid" and self.focus_terms:
            raise ValueError("focus terms require selection_mode='focus'.")
        if len(self.focus_terms) > 8:
            raise ValueError("focus selection accepts at most 8 terms.")
        if self.selection_mode != "financial_precision" and self.preferred_domains:
            raise ValueError("preferred_domains require financial_precision selection.")
        if len(self.preferred_domains) > 8:
            raise ValueError("financial_precision accepts at most 8 preferred domains.")
        normalized_domains: set[str] = set()
        for domain in self.preferred_domains:
            if (
                not isinstance(domain, str)
                or len(domain.strip()) > 253
                or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                    domain.strip().lower(),
                )
            ):
                raise ValueError("preferred_domains must contain bare DNS hostnames.")
            normalized_domain = domain.strip().lower()
            if normalized_domain in normalized_domains:
                raise ValueError("preferred_domains must be unique ignoring case.")
            normalized_domains.add(normalized_domain)

        normalized: set[str] = set()
        for term in self.focus_terms:
            if not isinstance(term, str) or not term.strip() or len(term) > 64:
                raise ValueError("each focus term must contain 1 to 64 characters.")
            if "\n" in term or "\r" in term:
                raise ValueError("focus terms must be single-line text.")
            lowered = term.strip().casefold()
            if lowered in normalized:
                raise ValueError("focus terms must be unique ignoring case.")
            normalized.add(lowered)

        if self.selection_mode == "financial_precision":
            if target_financial_year(self.focus_terms) is None:
                raise ValueError("financial_precision requires exactly one four-digit fiscal year.")
            if not any(term.casefold().strip() in {"revenue", "total revenue"} for term in self.focus_terms):
                raise ValueError("financial_precision currently supports total revenue only.")


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceDocumentChunks:
    """All deterministic chunks belonging to one source."""

    source_id: UUID
    chunks: tuple[DocumentChunk, ...]
    source_url: str | None = None
    retrieval_truncated: bool = False


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceChunkWorkItem:
    """One bounded chunk scheduled for model extraction."""

    source_id: UUID
    chunk: DocumentChunk


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceWorkloadPlan:
    """Deterministic bounded chunk schedule."""

    eligible_document_count: int
    selected_document_count: int
    available_chunk_count: int
    scheduled_chunk_count: int
    skipped_chunk_count: int
    items: tuple[EvidenceChunkWorkItem, ...]


def plan_evidence_workload(
    documents: Sequence[EvidenceDocumentChunks],
    settings: EvidenceWorkloadSettings,
) -> EvidenceWorkloadPlan:
    """Build a deterministic fair evidence workload."""

    nonempty_documents = [document for document in documents if document.chunks]

    available_chunk_count = sum(len(document.chunks) for document in nonempty_documents)

    if settings.selection_mode == "financial_precision":
        year = target_financial_year(settings.focus_terms)
        assert year is not None  # Checked by immutable settings validation.
        scored: list[tuple[EvidenceDocumentChunks, tuple[DocumentChunk, ...], tuple[int, int], bool]] = []
        for document in nonempty_documents:
            # The leading-year table is not enough when a cover or URL explicitly
            # identifies a different published annual-report edition.
            cover = next((chunk.content for chunk in document.chunks if chunk.start_offset == 0), "")
            edition_years = declared_report_edition_years(document.source_url, cover)
            if edition_years and edition_years != {year}:
                continue
            scores = {
                chunk.index: financial_precision_rank(chunk, year=year, focus_terms=settings.focus_terms)
                for chunk in document.chunks
            }
            # Strict selection applies to every scheduled chunk, not only
            # the highest-ranked one. Never send a later-year comparison
            # table or an unrelated overview to the model as a spare chunk.
            eligible_chunks = (chunk for chunk in document.chunks if scores[chunk.index][0] >= 3)
            chunks = tuple(
                sorted(
                    eligible_chunks,
                    key=lambda chunk: (-scores[chunk.index][0], -scores[chunk.index][1], chunk.index),
                )
            )
            if not chunks:
                continue
            scored.append(
                (
                    document,
                    chunks,
                    scores[chunks[0].index],
                    is_preferred_source(document.source_url, settings.preferred_domains),
                )
            )
        # Once minimum grounding is satisfied, a caller's explicit
        # authoritative-domain preference outranks cosmetic table quality.
        scored.sort(
            key=lambda item: (
                -int(item[3]),
                -item[2][0],
                -item[2][1],
                item[1][0].index,
                str(item[0].source_id),
            )
        )
        ranked = [(document, chunks) for document, chunks, _, _ in scored]
    elif settings.selection_mode == "focus":
        # Rank only *existing* chunks. Never stitch text, change character
        # offsets, or invent a new excerpt to satisfy the query.
        terms = tuple(term.strip().casefold() for term in settings.focus_terms)

        def hits(chunk: DocumentChunk) -> int:
            text = chunk.content.casefold()
            return sum(term in text for term in terms)

        ranked = [
            (
                document,
                tuple(sorted(document.chunks, key=lambda chunk: (-hits(chunk), chunk.index))),
            )
            for document in nonempty_documents
        ]
        # Prefer more distinct term hits; break equal scores by earlier
        # original chunk and finally the deterministic source UUID.
        ranked.sort(
            key=lambda pair: (
                -hits(pair[1][0]),
                pair[1][0].index,
                str(pair[0].source_id),
            )
        )
    else:
        # Production default stays byte-for-byte equivalent to the original
        # UUID-based, prefix-chunk planning policy.
        ranked = [(document, document.chunks) for document in nonempty_documents]
        ranked.sort(key=lambda pair: str(pair[0].source_id))

    selected = ranked[: settings.max_documents]
    bounded_chunks = [chunks[: settings.max_chunks_per_document] for _, chunks in selected]

    items: list[EvidenceChunkWorkItem] = []

    # Round-robin scheduling prevents one very
    # large document from consuming the entire
    # run-level chunk budget.
    for chunk_index in range(settings.max_chunks_per_document):
        for (document, _), chunks in zip(
            selected,
            bounded_chunks,
            strict=True,
        ):
            if len(items) >= settings.max_total_chunks:
                break

            if chunk_index >= len(chunks):
                continue

            items.append(
                EvidenceChunkWorkItem(
                    source_id=(document.source_id),
                    chunk=chunks[chunk_index],
                )
            )

        if len(items) >= settings.max_total_chunks:
            break

    scheduled_chunk_count = len(items)

    return EvidenceWorkloadPlan(
        eligible_document_count=len(nonempty_documents),
        selected_document_count=len(selected),
        available_chunk_count=(available_chunk_count),
        scheduled_chunk_count=(scheduled_chunk_count),
        skipped_chunk_count=(available_chunk_count - scheduled_chunk_count),
        items=tuple(items),
    )
