from __future__ import annotations

import pytest

from enterprise_research.application import (
    DocumentChunkingSettings,
    chunk_document,
)


def test_empty_document_has_no_chunks() -> None:
    assert (
        chunk_document(
            "",
            DocumentChunkingSettings(),
        )
        == []
    )


def test_short_document_has_one_exact_chunk() -> None:
    content = "abcdef"

    chunks = chunk_document(
        content,
        DocumentChunkingSettings(
            chunk_size_chars=10,
            overlap_chars=2,
        ),
    )

    assert len(chunks) == 1

    assert chunks[0].content == content

    assert chunks[0].start_offset == 0

    assert chunks[0].end_offset == len(content)


def test_long_document_uses_deterministic_overlap() -> None:
    content = "abcdefghijklmnopqrst"

    chunks = chunk_document(
        content,
        DocumentChunkingSettings(
            chunk_size_chars=8,
            overlap_chars=2,
        ),
    )

    assert [
        (
            chunk.index,
            chunk.start_offset,
            chunk.end_offset,
            chunk.content,
        )
        for chunk in chunks
    ] == [
        (
            0,
            0,
            8,
            "abcdefgh",
        ),
        (
            1,
            6,
            14,
            "ghijklmn",
        ),
        (
            2,
            12,
            20,
            "mnopqrst",
        ),
    ]


def test_chunking_reconstructs_every_character() -> None:
    content = "0123456789abcdefghijKLMNOPQRST"

    chunks = chunk_document(
        content,
        DocumentChunkingSettings(
            chunk_size_chars=12,
            overlap_chars=3,
        ),
    )

    covered = set()

    for chunk in chunks:
        covered.update(
            range(
                chunk.start_offset,
                chunk.end_offset,
            )
        )

        assert content[chunk.start_offset : chunk.end_offset] == chunk.content

    assert covered == set(range(len(content)))


def test_invalid_chunk_settings_are_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="chunk_size_chars",
    ):
        DocumentChunkingSettings(
            chunk_size_chars=0,
        )

    with pytest.raises(
        ValueError,
        match="overlap_chars",
    ):
        DocumentChunkingSettings(
            chunk_size_chars=100,
            overlap_chars=100,
        )
