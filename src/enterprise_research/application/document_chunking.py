"""Deterministic chunking for retrieved research documents."""

from __future__ import annotations

from dataclasses import dataclass

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
)


@dataclass(
    frozen=True,
    slots=True,
)
class DocumentChunkingSettings:
    """Character-based chunking settings."""

    chunk_size_chars: int = 20_000
    overlap_chars: int = 1_000

    def __post_init__(self) -> None:
        if self.chunk_size_chars < 1:
            raise ValueError("chunk_size_chars must be at least 1.")

        if self.overlap_chars < 0:
            raise ValueError("overlap_chars must be non-negative.")

        if self.overlap_chars >= self.chunk_size_chars:
            raise ValueError("overlap_chars must be smaller than chunk_size_chars.")


def chunk_document(
    content: str,
    settings: DocumentChunkingSettings,
) -> list[DocumentChunk]:
    """Split text into deterministic overlapping character ranges."""
    if not content:
        return []

    chunks: list[DocumentChunk] = []

    start = 0
    index = 0
    length = len(content)

    while start < length:
        end = min(
            start + settings.chunk_size_chars,
            length,
        )

        chunks.append(
            DocumentChunk(
                index=index,
                start_offset=start,
                end_offset=end,
                content=content[start:end],
            )
        )

        if end == length:
            break

        start = end - settings.overlap_chars

        index += 1

    return chunks
