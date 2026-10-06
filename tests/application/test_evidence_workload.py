from __future__ import annotations

from uuid import UUID

import pytest

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
)
from enterprise_research.application.evidence_workload import (
    EvidenceDocumentChunks,
    EvidenceWorkloadSettings,
    plan_evidence_workload,
)


def make_chunk(
    index: int,
) -> DocumentChunk:
    start = index * 10

    return DocumentChunk(
        index=index,
        start_offset=start,
        end_offset=start + 10,
        content="x" * 10,
    )


def make_document(
    source_number: int,
    chunk_count: int,
) -> EvidenceDocumentChunks:
    return EvidenceDocumentChunks(
        source_id=UUID(int=source_number),
        chunks=tuple(make_chunk(index) for index in range(chunk_count)),
    )


def test_round_robin_prevents_one_document_from_monopolizing_budget() -> None:
    plan = plan_evidence_workload(
        [
            make_document(1, 5),
            make_document(2, 5),
        ],
        EvidenceWorkloadSettings(
            max_documents=2,
            max_chunks_per_document=3,
            max_total_chunks=4,
        ),
    )

    assert [
        (
            item.source_id.int,
            item.chunk.index,
        )
        for item in plan.items
    ] == [
        (1, 0),
        (2, 0),
        (1, 1),
        (2, 1),
    ]

    assert plan.available_chunk_count == 10

    assert plan.scheduled_chunk_count == 4

    assert plan.skipped_chunk_count == 6


def test_per_document_limit_is_enforced() -> None:
    plan = plan_evidence_workload(
        [
            make_document(1, 10),
            make_document(2, 1),
        ],
        EvidenceWorkloadSettings(
            max_documents=2,
            max_chunks_per_document=2,
            max_total_chunks=10,
        ),
    )

    assert [
        (
            item.source_id.int,
            item.chunk.index,
        )
        for item in plan.items
    ] == [
        (1, 0),
        (2, 0),
        (1, 1),
    ]

    assert plan.scheduled_chunk_count == 3

    assert plan.skipped_chunk_count == 8


def test_document_limit_is_enforced_deterministically() -> None:
    plan = plan_evidence_workload(
        [
            make_document(3, 1),
            make_document(1, 1),
            make_document(2, 1),
        ],
        EvidenceWorkloadSettings(
            max_documents=2,
            max_chunks_per_document=1,
            max_total_chunks=10,
        ),
    )

    assert [item.source_id.int for item in plan.items] == [
        1,
        2,
    ]

    assert plan.eligible_document_count == 3

    assert plan.selected_document_count == 2

    assert plan.skipped_chunk_count == 1


def test_empty_documents_do_not_consume_document_budget() -> None:
    plan = plan_evidence_workload(
        [
            make_document(1, 0),
            make_document(2, 1),
        ],
        EvidenceWorkloadSettings(
            max_documents=1,
            max_chunks_per_document=1,
            max_total_chunks=1,
        ),
    )

    assert plan.eligible_document_count == 1

    assert plan.selected_document_count == 1

    assert [item.source_id.int for item in plan.items] == [
        2,
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {
            "max_documents": 0,
        },
        {
            "max_chunks_per_document": 0,
        },
        {
            "max_total_chunks": 0,
        },
    ],
)
def test_invalid_workload_limits_are_rejected(
    settings: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        EvidenceWorkloadSettings(**settings)
