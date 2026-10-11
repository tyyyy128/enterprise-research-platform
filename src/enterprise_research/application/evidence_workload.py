"""Deterministic workload planning for evidence extraction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
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

    def __post_init__(self) -> None:
        if self.max_documents < 1:
            raise ValueError("max_documents must be at least 1.")

        if self.max_chunks_per_document < 1:
            raise ValueError("max_chunks_per_document must be at least 1.")

        if self.max_total_chunks < 1:
            raise ValueError("max_total_chunks must be at least 1.")

        if self.selection_mode not in {"uuid", "focus"}:
            raise ValueError("selection_mode must be 'uuid' or 'focus'.")
        if self.selection_mode == "focus" and not self.focus_terms:
            raise ValueError("focus selection requires at least one focus term.")
        if self.selection_mode == "uuid" and self.focus_terms:
            raise ValueError("focus terms require selection_mode='focus'.")
        if len(self.focus_terms) > 8:
            raise ValueError("focus selection accepts at most 8 terms.")

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


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceDocumentChunks:
    """All deterministic chunks belonging to one source."""

    source_id: UUID
    chunks: tuple[DocumentChunk, ...]


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

    if settings.selection_mode == "focus":
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
