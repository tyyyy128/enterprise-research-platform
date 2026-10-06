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

    def __post_init__(self) -> None:
        if self.max_documents < 1:
            raise ValueError("max_documents must be at least 1.")

        if self.max_chunks_per_document < 1:
            raise ValueError("max_chunks_per_document must be at least 1.")

        if self.max_total_chunks < 1:
            raise ValueError("max_total_chunks must be at least 1.")


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

    ordered_documents = sorted(
        nonempty_documents,
        key=lambda document: str(document.source_id),
    )

    selected_documents = ordered_documents[: settings.max_documents]

    available_chunk_count = sum(len(document.chunks) for document in nonempty_documents)

    bounded_chunks = [document.chunks[: settings.max_chunks_per_document] for document in selected_documents]

    items: list[EvidenceChunkWorkItem] = []

    # Round-robin scheduling prevents one very
    # large document from consuming the entire
    # run-level chunk budget.
    for chunk_index in range(settings.max_chunks_per_document):
        for document, chunks in zip(
            selected_documents,
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
        selected_document_count=len(selected_documents),
        available_chunk_count=(available_chunk_count),
        scheduled_chunk_count=(scheduled_chunk_count),
        skipped_chunk_count=(available_chunk_count - scheduled_chunk_count),
        items=tuple(items),
    )
