"""Mechanical verification of LLM-proposed evidence excerpts."""

from __future__ import annotations

from dataclasses import dataclass

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
    EvidenceCandidate,
)


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceCandidate:
    """A candidate proven to occur exactly once in one document chunk."""

    content: str
    relevance_score: float
    start_offset: int
    end_offset: int


def verify_evidence_candidate(
    candidate: EvidenceCandidate,
    chunk: DocumentChunk,
) -> VerifiedEvidenceCandidate | None:
    """Resolve one exact, unambiguous excerpt to absolute offsets."""
    first = chunk.content.find(candidate.content)

    if first < 0:
        return None

    second = chunk.content.find(
        candidate.content,
        first + 1,
    )

    if second >= 0:
        return None

    start_offset = chunk.start_offset + first

    end_offset = start_offset + len(candidate.content)

    return VerifiedEvidenceCandidate(
        content=candidate.content,
        relevance_score=(candidate.relevance_score),
        start_offset=start_offset,
        end_offset=end_offset,
    )
