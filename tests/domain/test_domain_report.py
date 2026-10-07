from uuid import uuid4

from enterprise_research.domain.report import (
    ReportStatus,
    ResearchReport,
)


def test_report_lifecycle():

    report = ResearchReport(
        task_id=uuid4(),
        title="Industry Research Report",
    )


    assert report.status == (
        ReportStatus.DRAFT
    )


    claim_id = uuid4()

    report.add_claim(
        claim_id
    )


    assert claim_id in (
        report.claim_ids
    )


    report.start_review()

    assert report.status == (
        ReportStatus.REVIEWING
    )


    report.complete()

    assert report.status == (
        ReportStatus.COMPLETED
    )
