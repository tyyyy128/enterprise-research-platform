from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID, uuid4


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass
class Evidence:

    source_id: UUID

    quote: str

    id: UUID = field(
        default_factory=uuid4
    )

    locator: str | None = None

    confidence: float = 0.0

    verification_status: VerificationStatus = (
        VerificationStatus.PENDING
    )


    def verify(self):
        self.verification_status = (
            VerificationStatus.VERIFIED
        )


    def reject(self):
        self.verification_status = (
            VerificationStatus.REJECTED
        )
