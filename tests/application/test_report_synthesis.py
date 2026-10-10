"""3H bounded model outline and evidence-grounded report rendering."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from enterprise_research.application.errors import ResearchReportWritingError
from enterprise_research.application.report_synthesis import (
    ReportSynthesisCaveat,
    ReportSynthesisContext,
    ReportSynthesisPlan,
    ReportSynthesisSection,
    ResearchReportSynthesisService,
    plan_data,
    plan_from_data,
    synthesis_input_sha256,
    synthesis_receipt_sha256,
    validate_synthesis_plan,
)
from enterprise_research.application.report_writing import (
    ReportClaimReference,
    ReportEvidenceReference,
    render_synthesized_report,
    report_digest,
)
from enterprise_research.application.run_lease import ResearchRunLease


def context():
    def ref(text):
        return ReportClaimReference(
            uuid4(),
            text,
            (
                ReportEvidenceReference(
                    uuid4(), uuid4(), "https://example.org/source", "chars:1-20", "a real excerpt", "a" * 64
                ),
            ),
        )

    claims = (ref("First observed result"), ref("Second corroborating result"))
    caveats = (
        ReportSynthesisCaveat(uuid4(), "A speculative outcome", "unsupported", "Not enough evidence."),
        ReportSynthesisCaveat(uuid4(), "<script>conflicted</script>", "conflicted", "Sources disagree."),
    )
    title = "Detailed research"
    return ReportSynthesisContext(
        uuid4(),
        uuid4(),
        7,
        title,
        claims,
        caveats,
        synthesis_input_sha256(title, claims, caveats),
    )


def plan(ctx):
    return ReportSynthesisPlan(
        (ctx.claims[1].claim_id,),
        (
            ReportSynthesisSection("Key findings", (ctx.claims[1].claim_id,)),
            ReportSynthesisSection("Related findings", (ctx.claims[0].claim_id,)),
        ),
    )


def test_model_grouped_outline_is_deterministic_and_preserves_exact_facts_and_caveats():
    ctx = context()
    proposed = plan(ctx)
    first = render_synthesized_report(ctx, proposed)
    assert first == render_synthesized_report(ctx, proposed)
    assert "## Executive overview" in first.body
    assert first.body.index("Second corroborating result") < first.body.index("First observed result")
    assert "## Conflicts and evidence limitations" in first.body
    assert "speculative outcome" in first.body
    assert "&lt;script&gt;conflicted&lt;/script&gt;" in first.body
    assert "<script>" not in first.body
    assert len(first.citations) == 2
    assert {item["claim_id"] for item in first.citations} == {str(c.claim_id) for c in ctx.claims}
    assert hashlib.sha256(first.body.encode()).hexdigest() == first.body_sha256
    assert first.payload_sha256 == report_digest(first.title, first.body, first.citations)
    assert plan_from_data(plan_data(proposed)) == proposed
    assert synthesis_receipt_sha256("mock", ctx.input_sha256, proposed) != synthesis_receipt_sha256(
        "another", ctx.input_sha256, proposed
    )


@pytest.mark.parametrize(
    "change",
    [
        "foreign",
        "duplicate",
        "omitted",
        "malicious_heading",
        "duplicate_heading",
        "invalid_highlight",
        "empty",
    ],
)
def test_synthesis_rejects_unapproved_or_missing_claims(change):
    ctx = context()
    a, b = (claim.claim_id for claim in ctx.claims)
    if change == "foreign":
        proposed = ReportSynthesisPlan((), (ReportSynthesisSection("Key findings", (a, uuid4())),))
    elif change == "duplicate":
        proposed = ReportSynthesisPlan((), (ReportSynthesisSection("Key findings", (a, a, b)),))
    elif change == "omitted":
        proposed = ReportSynthesisPlan((), (ReportSynthesisSection("Key findings", (a,)),))
    elif change == "malicious_heading":
        proposed = ReportSynthesisPlan((), (ReportSynthesisSection("<h1>invented fact</h1>", (a, b)),))
    elif change == "duplicate_heading":
        proposed = ReportSynthesisPlan(
            (), (ReportSynthesisSection("Key findings", (a,)), ReportSynthesisSection("Key findings", (b,)))
        )
    elif change == "invalid_highlight":
        proposed = ReportSynthesisPlan((uuid4(),), (ReportSynthesisSection("Key findings", (a, b)),))
    else:
        proposed = ReportSynthesisPlan((), ())
    with pytest.raises(ResearchReportWritingError, match="synthesis"):
        validate_synthesis_plan(ctx, proposed)


def test_persisted_plan_decoder_is_strict():
    ctx = context()
    data = plan_data(plan(ctx))
    for mutation in (
        {**data, "unexpected": "injected"},
        {**data, "sections": [{"heading": "Key findings", "claim_ids": [None]}]},
        {**data, "highlights": ["not-uuid"]},
    ):
        with pytest.raises(ResearchReportWritingError, match="malformed"):
            plan_from_data(mutation)


def test_model_is_called_once_and_commit_uses_immutable_context():
    async def exercise():
        ctx = context()
        chosen = plan(ctx)
        calls = []

        class Repository:
            async def load_synthesis_context(self, task_id, run_id, lease):
                calls.append("load")
                return ctx

            async def write_synthesized_guarded(self, original, proposal, lease, *, model):
                calls.append((original, proposal, model))

        class Planner:
            async def compose(self, original):
                calls.append("model")
                return chosen

        # The application only reads the lease's run_id; the PostgreSQL repository checks token/expiry.
        lease = ResearchRunLease(ctx.run_id, uuid4(), ctx.fencing_token, datetime.now(UTC) + timedelta(seconds=30))
        await ResearchReportSynthesisService(Repository(), Planner(), model="mock-3h").write_run(
            ctx.task_id, ctx.run_id, lease=lease
        )
        assert calls == ["load", "model", (ctx, chosen, "mock-3h")]
        with pytest.raises(ResearchReportWritingError, match="fenced"):
            await ResearchReportSynthesisService(Repository(), Planner(), model="mock-3h").write_run(
                ctx.task_id, ctx.run_id, lease=None
            )

    asyncio.run(exercise())
