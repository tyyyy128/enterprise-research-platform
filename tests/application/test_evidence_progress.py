from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from enterprise_research.application.evidence_progress import (
    EvidenceExtractionProgress,
    emit_evidence_progress,
)


def make_progress(
    **overrides: int,
) -> EvidenceExtractionProgress:
    values = {
        "eligible_document_count": 15,
        "selected_document_count": 4,
        "available_chunk_count": 61,
        "scheduled_chunk_count": 24,
        "skipped_chunk_count": 37,
        "completed_chunk_count": 11,
        "candidate_count": 11,
        "verified_count": 8,
        "rejected_count": 3,
        "deduplicated_count": 1,
        "created_count": 5,
        "reused_count": 2,
    }

    values.update(overrides)

    return EvidenceExtractionProgress(
        task_id=uuid4(),
        run_id=uuid4(),
        **values,
    )


def test_progress_exposes_remaining_work_and_completion_ratio() -> None:
    progress = make_progress()

    assert progress.remaining_chunk_count == 13

    assert progress.completion_ratio == pytest.approx(11 / 24)


def test_progress_treats_zero_scheduled_work_as_complete() -> None:
    progress = make_progress(
        available_chunk_count=0,
        scheduled_chunk_count=0,
        skipped_chunk_count=0,
        completed_chunk_count=0,
        candidate_count=0,
        verified_count=0,
        rejected_count=0,
        deduplicated_count=0,
        created_count=0,
        reused_count=0,
    )

    assert progress.remaining_chunk_count == 0

    assert progress.completion_ratio == 1.0


@pytest.mark.parametrize(
    (
        "overrides",
        "match",
    ),
    [
        (
            {
                "candidate_count": -1,
            },
            "non-negative",
        ),
        (
            {
                "selected_document_count": 16,
            },
            "selected_document_count",
        ),
        (
            {
                "available_chunk_count": 10,
                "scheduled_chunk_count": 11,
                "skipped_chunk_count": 0,
            },
            "scheduled_chunk_count",
        ),
        (
            {
                "skipped_chunk_count": 36,
            },
            "skipped_chunk_count",
        ),
        (
            {
                "completed_chunk_count": 25,
            },
            "completed_chunk_count",
        ),
        (
            {
                "candidate_count": 10,
            },
            "verified plus rejected",
        ),
        (
            {
                "created_count": 6,
            },
            "evidence outcomes",
        ),
    ],
)
def test_progress_rejects_inconsistent_snapshots(
    overrides: dict[str, int],
    match: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=match,
    ):
        make_progress(**overrides)


def test_emit_evidence_progress_calls_async_callback() -> None:
    async def run_test() -> None:
        received: list[EvidenceExtractionProgress] = []

        async def callback(
            progress: (EvidenceExtractionProgress),
        ) -> None:
            received.append(progress)

        progress = make_progress()

        await emit_evidence_progress(
            callback,
            progress,
        )

        assert received == [progress]

    asyncio.run(run_test())


def test_emit_evidence_progress_allows_no_callback() -> None:
    asyncio.run(
        emit_evidence_progress(
            None,
            make_progress(),
        )
    )


def test_emit_evidence_progress_propagates_callback_failure() -> None:
    async def run_test() -> None:
        async def callback(
            _progress: (EvidenceExtractionProgress),
        ) -> None:
            raise RuntimeError("progress sink failed")

        with pytest.raises(
            RuntimeError,
            match=("progress sink failed"),
        ):
            await emit_evidence_progress(
                callback,
                make_progress(),
            )

    asyncio.run(run_test())
