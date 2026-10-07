from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID, uuid4


class QuestionStatus(str, Enum):
    PENDING = "pending"
    RESEARCHING = "researching"
    ANSWERED = "answered"
    FAILED = "failed"


@dataclass
class ResearchQuestion:

    task_id: UUID

    question: str

    id: UUID = field(
        default_factory=uuid4
    )

    status: QuestionStatus = (
        QuestionStatus.PENDING
    )


    def start(self):
        self.status = (
            QuestionStatus.RESEARCHING
        )


    def answer(self):
        self.status = (
            QuestionStatus.ANSWERED
        )


    def fail(self):
        self.status = (
            QuestionStatus.FAILED
        )
