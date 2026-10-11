from __future__ import annotations

from uuid import UUID

import pytest

from enterprise_research.application.document_chunking import DocumentChunkingSettings, chunk_document
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


def test_focus_mode_picks_relevant_source_in_one_chunk_budget() -> None:
    # The old UUID/prefix strategy would select the first Scribd-like
    # document and never send the available official excerpt to the model.
    background = EvidenceDocumentChunks(
        source_id=UUID(int=1),
        chunks=(
            DocumentChunk(index=0, start_offset=0, end_offset=10, content="unrelated!"),
            DocumentChunk(index=1, start_offset=10, end_offset=25, content="total revenue!!"),
        ),
    )
    official = EvidenceDocumentChunks(
        source_id=UUID(int=2),
        chunks=(
            DocumentChunk(index=0, start_offset=0, end_offset=20, content="2024 revenue: $100M!"),
            DocumentChunk(index=1, start_offset=20, end_offset=30, content="other text"),
        ),
    )
    plan = plan_evidence_workload(
        (background, official),
        EvidenceWorkloadSettings(
            max_documents=1,
            max_chunks_per_document=1,
            max_total_chunks=1,
            selection_mode="focus",
            focus_terms=("revenue", "2024"),
        ),
    )
    assert [(item.source_id.int, item.chunk.index) for item in plan.items] == [(2, 0)]
    assert (plan.eligible_document_count, plan.available_chunk_count) == (2, 4)
    assert (plan.scheduled_chunk_count, plan.skipped_chunk_count) == (1, 3)


def test_focus_mode_replays_observed_3i2_keyword_positions_without_network() -> None:
    # The strings below reproduce SQL-reported positions, not real page text.
    scribd = "x" * 3051 + "2024" + "x" * 220 + "revenue"
    microsoft = "x" * 27 + "2024" + "x" * 1387 + "revenue"
    stocklight = "x" * 81 + "2024" + "x" * 2500

    settings = DocumentChunkingSettings(chunk_size_chars=2500, overlap_chars=0)
    documents = (
        EvidenceDocumentChunks(UUID(int=1), tuple(chunk_document(scribd, settings))),
        EvidenceDocumentChunks(UUID(int=2), tuple(chunk_document(microsoft, settings))),
        EvidenceDocumentChunks(UUID(int=3), tuple(chunk_document(stocklight, settings))),
    )
    plan = plan_evidence_workload(
        documents,
        EvidenceWorkloadSettings(
            max_documents=1,
            max_chunks_per_document=1,
            max_total_chunks=1,
            selection_mode="focus",
            focus_terms=("2024", "revenue"),
        ),
    )
    assert len(plan.items) == 1
    assert plan.items[0].source_id == UUID(int=2)
    assert plan.items[0].chunk.index == 0
    assert "revenue" in plan.items[0].chunk.content
    assert plan.scheduled_chunk_count == 1


def test_focus_mode_selects_later_chunk_without_rewriting_offsets() -> None:
    chunks = (
        DocumentChunk(index=0, start_offset=0, end_offset=6, content="header"),
        DocumentChunk(index=1, start_offset=6, end_offset=16, content="revenue123"),
    )
    plan = plan_evidence_workload(
        (EvidenceDocumentChunks(source_id=UUID(int=3), chunks=chunks),),
        EvidenceWorkloadSettings(
            max_documents=1,
            max_chunks_per_document=1,
            max_total_chunks=1,
            selection_mode="focus",
            focus_terms=("REVENUE",),
        ),
    )
    assert len(plan.items) == 1
    assert plan.items[0].chunk is chunks[1]
    assert plan.items[0].chunk.start_offset == 6
    assert plan.items[0].chunk.end_offset == 16


def test_focus_mode_without_hits_falls_back_to_uuid_and_prefix() -> None:
    plan = plan_evidence_workload(
        (make_document(3, 3), make_document(1, 2)),
        EvidenceWorkloadSettings(
            max_documents=1,
            max_chunks_per_document=1,
            max_total_chunks=1,
            selection_mode="focus",
            focus_terms=("nonexistent",),
        ),
    )
    assert [(item.source_id.int, item.chunk.index) for item in plan.items] == [(1, 0)]


@pytest.mark.parametrize(
    ("mode", "terms"),
    [
        ("unknown", ()),
        ("focus", ()),
        ("uuid", ("revenue",)),
        ("focus", ("  ",)),
        ("focus", ("x" * 65,)),
        ("focus", ("a", "A")),
        ("focus", tuple(str(i) for i in range(9))),
        ("focus", ("line\nbreak",)),
    ],
)
def test_focus_config_must_be_explicit_and_bounded(mode: str, terms: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        EvidenceWorkloadSettings(selection_mode=mode, focus_terms=terms)
