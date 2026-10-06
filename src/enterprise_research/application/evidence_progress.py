"""Progress contracts for evidence extraction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID


@dataclass(
    frozen=True,
    slots=True,
)
class EvidenceExtractionProgress:
    """Immutable progress snapshot for one evidence extraction stage."""

    task_id: UUID
    run_id: UUID

    eligible_document_count: int
    selected_document_count: int

    available_chunk_count: int
    scheduled_chunk_count: int
    skipped_chunk_count: int
    completed_chunk_count: int

    candidate_count: int
    verified_count: int
    rejected_count: int

    deduplicated_count: int
    created_count: int
    reused_count: int

    def __post_init__(
        self,
    ) -> None:
        counts = (
            self.eligible_document_count,
            self.selected_document_count,
            self.available_chunk_count,
            self.scheduled_chunk_count,
            self.skipped_chunk_count,
            self.completed_chunk_count,
            self.candidate_count,
            self.verified_count,
            self.rejected_count,
            self.deduplicated_count,
            self.created_count,
            self.reused_count,
        )

        if any(value < 0 for value in counts):
            raise ValueError("evidence progress counts must be non-negative")

        if self.selected_document_count > self.eligible_document_count:
            raise ValueError("selected_document_count cannot exceed eligible_document_count")

        if self.scheduled_chunk_count > self.available_chunk_count:
            raise ValueError("scheduled_chunk_count cannot exceed available_chunk_count")

        if self.skipped_chunk_count != (self.available_chunk_count - self.scheduled_chunk_count):
            raise ValueError("skipped_chunk_count must equal available minus scheduled")

        if self.completed_chunk_count > self.scheduled_chunk_count:
            raise ValueError("completed_chunk_count cannot exceed scheduled_chunk_count")

        if self.verified_count + self.rejected_count > self.candidate_count:
            raise ValueError("verified plus rejected candidates cannot exceed candidate_count")

        if self.created_count + self.reused_count + self.deduplicated_count > self.verified_count:
            raise ValueError("evidence outcomes cannot exceed verified_count")

    @property
    def remaining_chunk_count(
        self,
    ) -> int:
        """Return scheduled model calls that have not completed yet."""
        return self.scheduled_chunk_count - self.completed_chunk_count

    @property
    def completion_ratio(
        self,
    ) -> float:
        """Return model-call completion in the range [0.0, 1.0]."""
        if self.scheduled_chunk_count == 0:
            return 1.0

        return self.completed_chunk_count / self.scheduled_chunk_count


EvidenceProgressCallback = Callable[
    [EvidenceExtractionProgress],
    Awaitable[None],
]


async def emit_evidence_progress(
    callback: (EvidenceProgressCallback | None),
    progress: EvidenceExtractionProgress,
) -> None:
    """Publish one progress snapshot when a callback is configured."""
    if callback is None:
        return

    await callback(progress)
