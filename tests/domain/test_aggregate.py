from __future__ import annotations

from uuid import uuid4

import pytest

from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    InvalidReportError,
    ResearchAggregate,
    ResearchTaskStatus,
    Source,
    SourceType,
    UnknownEvidenceError,
    UnknownSourceError,
)


def create_source() -> Source:
    return Source(
        source_type=SourceType.WEB,
        title="LangGraph documentation",
        url="https://example.com/langgraph",
    )


def test_evidence_requires_known_source() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    evidence = Evidence(
        source_id=uuid4(),
        content="LangGraph supports durable execution.",
    )

    with pytest.raises(UnknownSourceError):
        aggregate.add_evidence(evidence)


def test_claim_requires_known_evidence() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    claim = Claim(
        text="LangGraph supports durable execution.",
        evidence_ids=[uuid4()],
    )

    with pytest.raises(UnknownEvidenceError):
        aggregate.add_claim(claim)


def test_claim_can_be_traced_to_evidence_and_source() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    source = aggregate.add_source(create_source())

    evidence = aggregate.add_evidence(
        Evidence(
            source_id=source.source_id,
            content="LangGraph supports durable execution.",
            locator="section: durable-execution",
        )
    )

    claim = aggregate.add_claim(
        Claim(
            text="LangGraph supports durable execution.",
            evidence_ids=[evidence.evidence_id],
        )
    )

    lineage = aggregate.trace_claim(claim.claim_id)

    assert lineage == [(evidence, source)]


def test_support_claim_marks_claim_as_supported() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    source = aggregate.add_source(create_source())

    evidence = aggregate.add_evidence(
        Evidence(
            source_id=source.source_id,
            content="LangGraph supports durable execution.",
        )
    )

    claim = aggregate.add_claim(
        Claim(
            text="LangGraph supports durable execution.",
        )
    )

    updated = aggregate.support_claim(
        claim.claim_id,
        [evidence.evidence_id],
        confidence=0.95,
    )

    assert updated.status == ClaimStatus.SUPPORTED
    assert updated.evidence_ids == [evidence.evidence_id]
    assert updated.confidence == 0.95


def test_report_rejects_unsupported_claim() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    claim = aggregate.add_claim(
        Claim(
            text="An unverified statement.",
        )
    )

    with pytest.raises(InvalidReportError):
        aggregate.build_report(
            title="Research Report",
            claim_ids=[claim.claim_id],
        )


def test_report_contains_only_supported_claims() -> None:
    aggregate = ResearchAggregate.create("Research LangGraph")

    source = aggregate.add_source(create_source())

    evidence = aggregate.add_evidence(
        Evidence(
            source_id=source.source_id,
            content="LangGraph supports durable execution.",
        )
    )

    claim = aggregate.add_claim(
        Claim(
            text="LangGraph supports durable execution.",
        )
    )

    supported_claim = aggregate.support_claim(
        claim.claim_id,
        [evidence.evidence_id],
        confidence=0.95,
    )

    report = aggregate.build_report(
        title="LangGraph Research Report",
    )

    assert report.claim_ids == [supported_claim.claim_id]
    assert aggregate.task.report_id == report.report_id
    assert aggregate.task.status == ResearchTaskStatus.COMPLETED
