from uuid import uuid4

from enterprise_research.domain.claim import (
    Claim,
    ClaimStatus,
)


def test_claim_lifecycle():

    claim = Claim(
        statement="Market growth will continue."
    )


    assert claim.status == (
        ClaimStatus.DRAFT
    )


    evidence_id = uuid4()

    claim.add_evidence(
        evidence_id
    )

    assert evidence_id in (
        claim.evidence_ids
    )


    claim.verify()


    assert claim.status == (
        ClaimStatus.VERIFIED
    )
