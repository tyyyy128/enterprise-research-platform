from __future__ import annotations

from enterprise_research.application import (
    DocumentChunk,
    EvidenceCandidate,
    verify_evidence_candidate,
)


def test_exact_unique_excerpt_resolves_absolute_offsets() -> None:
    chunk = DocumentChunk(
        index=2,
        start_offset=100,
        end_offset=121,
        content="alpha target omega!!!",
    )

    candidate = EvidenceCandidate(
        content="target",
        relevance_score=0.9,
    )

    verified = verify_evidence_candidate(
        candidate,
        chunk,
    )

    assert verified is not None

    assert verified.start_offset == 106

    assert verified.end_offset == 112

    assert verified.content == "target"


def test_missing_excerpt_is_rejected() -> None:
    chunk = DocumentChunk(
        index=0,
        start_offset=0,
        end_offset=11,
        content="hello world",
    )

    candidate = EvidenceCandidate(
        content="absent",
        relevance_score=0.5,
    )

    assert (
        verify_evidence_candidate(
            candidate,
            chunk,
        )
        is None
    )


def test_ambiguous_repeated_excerpt_is_rejected() -> None:
    chunk = DocumentChunk(
        index=0,
        start_offset=0,
        end_offset=11,
        content="abc abc abc",
    )

    candidate = EvidenceCandidate(
        content="abc",
        relevance_score=0.5,
    )

    assert (
        verify_evidence_candidate(
            candidate,
            chunk,
        )
        is None
    )


def test_verification_is_exact_not_whitespace_normalized() -> None:
    chunk = DocumentChunk(
        index=0,
        start_offset=0,
        end_offset=9,
        content="a  b text",
    )

    candidate = EvidenceCandidate(
        content="a b",
        relevance_score=0.5,
    )

    assert (
        verify_evidence_candidate(
            candidate,
            chunk,
        )
        is None
    )
