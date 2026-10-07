from uuid import uuid4

from enterprise_research.domain.evidence import (
    Evidence,
    VerificationStatus,
)


def test_evidence_lifecycle():

    evidence = Evidence(
        source_id=uuid4(),
        quote="Market growth is expected.",
    )


    assert evidence.verification_status == (
        VerificationStatus.PENDING
    )


    evidence.verify()


    assert evidence.verification_status == (
        VerificationStatus.VERIFIED
    )


def test_evidence_metadata():

    evidence = Evidence(
        source_id=uuid4(),
        quote="Revenue increased.",
        locator="page 10",
        confidence=0.9,
    )


    assert evidence.locator == "page 10"

    assert evidence.confidence == 0.9
