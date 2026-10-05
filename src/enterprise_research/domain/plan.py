"""Research planning domain models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_research.domain.models import utc_now


class ResearchQuestion(BaseModel):
    """One focused question in a research plan."""

    model_config = ConfigDict(str_strip_whitespace=True)

    question_id: UUID = Field(default_factory=uuid4)
    question: str = Field(min_length=1)
    rationale: str | None = Field(
        default=None,
        min_length=1,
    )


class ResearchPlan(BaseModel):
    """A structured plan for one research run."""

    model_config = ConfigDict(str_strip_whitespace=True)

    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID
    objective: str = Field(min_length=1)
    questions: list[ResearchQuestion] = Field(
        min_length=1,
    )
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_unique_questions(self) -> ResearchPlan:
        """Reject duplicate question IDs or duplicate question text."""
        question_ids = [question.question_id for question in self.questions]

        if len(question_ids) != len(set(question_ids)):
            raise ValueError("Research plan contains duplicate question IDs.")

        normalized_questions = [" ".join(question.question.split()).casefold() for question in self.questions]

        if len(normalized_questions) != len(set(normalized_questions)):
            raise ValueError("Research plan contains duplicate questions.")

        return self
