from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from uuid import UUID, uuid4


class ResearchStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ResearchTask:
    title: str
    description: str

    id: UUID = field(default_factory=uuid4)

    status: ResearchStatus = (
        ResearchStatus.CREATED
    )

    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )


    def start(self):
        self.status = ResearchStatus.RUNNING


    def complete(self):
        self.status = ResearchStatus.COMPLETED


    def fail(self):
        self.status = ResearchStatus.FAILED
