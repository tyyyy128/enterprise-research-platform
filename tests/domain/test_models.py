from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from enterprise_research.domain import (
    Claim,
    ClaimStatus,
    Evidence,
    ResearchReport,
    ResearchTask,
    Source,
    SourceType,
)


def test_source_and_evidence_are_traceable() -> None:
    source = Source(
        source_type=SourceType.WEB,
        title="LangGraph documentation",
        url="https://example.com/langgraph",
    )

    evidence = Evidence(
        source_id=source.source_id,
        content="LangGraph supports durable execution.",
        locator="section: durable-execution",
        relevance_score=0.95,
    )

    assert evidence.source_id == source.source_id
    assert evidence.relevance_score == 0.95


def test_supported_claim_requires_evidence() -> None:
    with pytest.raises(
        ValidationError,
        match="supported claim must reference",
    ):
        Claim(
            text="LangGraph supports durable execution.",
            status=ClaimStatus.SUPPORTED,
            confidence=0.9,
        )


def test_supported_claim_can_reference_evidence() -> None:
    evidence_id = uuid4()

    claim = Claim(
        text="LangGraph supports durable execution.",
        status=ClaimStatus.SUPPORTED,
        evidence_ids=[evidence_id],
        confidence=0.9,
    )

    assert claim.evidence_ids == [evidence_id]
    assert claim.status == ClaimStatus.SUPPORTED


def test_relevance_score_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            source_id=uuid4(),
            content="Evidence text",
            relevance_score=1.5,
        )


def test_report_links_back_to_task_and_claims() -> None:
    task = ResearchTask(query="How does LangGraph durable execution work?")

    claim_id = uuid4()

    report = ResearchReport(
        task_id=task.task_id,
        title="LangGraph Durable Execution",
        summary="A short research summary.",
        claim_ids=[claim_id],
    )

    assert report.task_id == task.task_id
    assert report.claim_ids == [claim_id]
