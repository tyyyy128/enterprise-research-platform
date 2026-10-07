from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID, uuid4


class ClaimStatus(str, Enum):
    DRAFT = "draft"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass
class Claim:

    statement: str

    id: UUID = field(
        default_factory=uuid4
    )

    confidence: float = 0.0

    status: ClaimStatus = (
        ClaimStatus.DRAFT
    )

    evidence_ids: list[UUID] = field(
        default_factory=list
    )


    def add_evidence(
        self,
        evidence_id: UUID,
    ):
        self.evidence_ids.append(
            evidence_id
        )


    def verify(self):
        self.status = (
            ClaimStatus.VERIFIED
        )


    def reject(self):
        self.status = (
            ClaimStatus.REJECTED
        )
