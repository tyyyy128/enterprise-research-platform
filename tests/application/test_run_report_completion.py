"""7C: deterministic report rendering and fail-closed stage dispatch."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from enterprise_research.application.errors import ResearchStageCompletionError, ResearchStageUnavailableError
from enterprise_research.application.pipeline import ResearchPipelineStageRunner
from enterprise_research.application.report_writing import (
    ReportClaimReference,
    ReportEvidenceReference,
    ResearchReportWritingService,
    ResearchRunReportArtifact,
    render_verified_report,
    report_digest,
)
from enterprise_research.domain import ResearchRunStatus


def test_verified_digest_is_deterministic_cited_and_escaped():
    citation = ReportEvidenceReference(
        evidence_id=uuid4(),
        source_id=uuid4(),
        source_url="https://example.org/primary",
        locator="chars:12-18",
        excerpt="<script>alert(1)</script>",
        fingerprint="a" * 64,
    )
    claim = ReportClaimReference(claim_id=uuid4(), text="Claim *based* on [source]", evidence=(citation,))
    a = render_verified_report("My Research", (claim,))
    b = render_verified_report("My Research", (claim,))
    assert a == b
    assert "&lt;script&gt;" in a.body and "<script>" not in a.body
    assert "Claim \\*based\\*" in a.body
    assert str(claim.claim_id) in str(a.citations)
    assert a.body_sha256 == hashlib.sha256(a.body.encode()).hexdigest()
    assert a.payload_sha256 == report_digest(a.title, a.body, a.citations)
    assert report_digest(a.title, a.body + "tampered", a.citations) != a.payload_sha256


def test_run_report_rejects_unbacked_or_duplicate_claims():
    with pytest.raises(ResearchStageCompletionError, match="supported"):
        render_verified_report("Topic", ())
    candidate = ReportClaimReference(claim_id=uuid4(), text="Claim", evidence=())
    with pytest.raises(ResearchStageCompletionError, match="unsupported"):
        render_verified_report("Topic", (candidate,))
    evidence = ReportEvidenceReference(uuid4(), uuid4(), "https://a.test/a", None, "A", "b" * 64)
    candidate = ReportClaimReference(claim_id=uuid4(), text="Claim", evidence=(evidence,))
    with pytest.raises(ResearchStageCompletionError, match="duplicate"):
        render_verified_report("Topic", (candidate, candidate))


def test_writing_pipeline_requires_writer_and_never_uses_noop():
    class FakePlanner:
        pass

    class Writer:
        def __init__(self):
            self.calls = []

        async def write_run(self, task_id, run_id, *, lease):
            self.calls.append((task_id, run_id, lease))

    async def exercise():
        tid, rid = uuid4(), uuid4()
        with pytest.raises(ResearchStageUnavailableError, match="no report writer configured"):
            await ResearchPipelineStageRunner(FakePlanner()).run_stage(tid, rid, ResearchRunStatus.WRITING)
        writer = Writer()
        pipeline = ResearchPipelineStageRunner(FakePlanner(), report_writing_service=writer)
        await pipeline.run_stage(tid, rid, ResearchRunStatus.WRITING)
        assert writer.calls == [(tid, rid, None)]

    asyncio.run(exercise())


def test_writer_requires_fencing_lease():
    class Repository:
        async def write_guarded(self, *_):
            raise AssertionError("Must fail before calling repository")

    async def exercise():
        with pytest.raises(ResearchStageCompletionError, match="fenced"):
            await ResearchReportWritingService(Repository()).write_run(uuid4(), uuid4(), lease=None)

    asyncio.run(exercise())


def test_report_response_round_trip():
    from enterprise_research.api.schemas import ResearchRunReportResponse

    draft = render_verified_report(
        "Topic",
        (
            ReportClaimReference(
                uuid4(),
                "Safe finding",
                (ReportEvidenceReference(uuid4(), uuid4(), "https://a.test", None, "x", "c" * 64),),
            ),
        ),
    )
    receipt = ResearchRunReportArtifact(
        task_id=uuid4(),
        run_id=uuid4(),
        fencing_token=8,
        title=draft.title,
        body=draft.body,
        citations=draft.citations,
        body_sha256=draft.body_sha256,
        payload_sha256=draft.payload_sha256,
        created_at=datetime.now(UTC),
    )
    response = ResearchRunReportResponse.from_artifact(receipt)
    assert response.run_id == receipt.run_id
    assert response.payload_sha256 == receipt.payload_sha256
    assert response.citations == list(receipt.citations)
