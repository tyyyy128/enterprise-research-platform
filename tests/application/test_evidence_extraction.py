from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise_research.application import (
    DocumentChunk,
    EvidenceCandidate,
)


def test_evidence_candidate_preserves_exact_content() -> None:
    content = "  Durable execution resumes after interruption.  "

    candidate = EvidenceCandidate(
        content=content,
        relevance_score=0.95,
    )

    assert candidate.content == content

    assert candidate.relevance_score == 0.95


def test_blank_evidence_candidate_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="must not be blank",
    ):
        EvidenceCandidate(
            content=" \n\t ",
            relevance_score=0.5,
        )


def test_relevance_score_is_bounded() -> None:
    with pytest.raises(
        ValidationError,
    ):
        EvidenceCandidate(
            content="Evidence",
            relevance_score=1.01,
        )


def test_document_chunk_validates_offsets() -> None:
    chunk = DocumentChunk(
        index=0,
        start_offset=10,
        end_offset=14,
        content="abcd",
    )

    assert chunk.end_offset - chunk.start_offset == len(chunk.content)


def test_document_chunk_rejects_length_mismatch() -> None:
    with pytest.raises(
        ValueError,
        match="length",
    ):
        DocumentChunk(
            index=0,
            start_offset=10,
            end_offset=15,
            content="abcd",
        )
