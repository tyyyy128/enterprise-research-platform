from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from uuid import UUID, uuid4


class ReportStatus(str, Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ResearchReport:

    task_id: UUID

    title: str

    id: UUID = field(
        default_factory=uuid4
    )

    summary: str | None = None

    claim_ids: list[UUID] = field(
        default_factory=list
    )

    status: ReportStatus = (
        ReportStatus.DRAFT
    )

    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )


    def add_claim(
        self,
        claim_id: UUID,
    ):
        self.claim_ids.append(
            claim_id
        )


    def start_review(self):
        self.status = (
            ReportStatus.REVIEWING
        )


    def complete(self):
        self.status = (
            ReportStatus.COMPLETED
        )


    def fail(self):
        self.status = (
            ReportStatus.FAILED
        )
