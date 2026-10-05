from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from pydantic import ValidationError

from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
)


def test_research_question_strips_text() -> None:
    question = ResearchQuestion(
        question="  What is durable execution?  ",
        rationale="  Establish the core mechanism.  ",
    )

    assert question.question == "What is durable execution?"
    assert question.rationale == "Establish the core mechanism."


def test_research_plan_preserves_order_and_links_to_run() -> None:
    task_id = uuid4()
    run_id = uuid4()

    first = ResearchQuestion(question="What is the core mechanism?")
    second = ResearchQuestion(question="How does recovery work?")

    plan = ResearchPlan(
        task_id=task_id,
        run_id=run_id,
        objective=("  Assess durable execution for enterprise agents.  "),
        questions=[
            first,
            second,
        ],
    )

    assert plan.task_id == task_id
    assert plan.run_id == run_id
    assert plan.objective == ("Assess durable execution for enterprise agents.")

    assert [item.question for item in plan.questions] == [
        "What is the core mechanism?",
        "How does recovery work?",
    ]

    assert plan.created_at.tzinfo == UTC


def test_research_plan_requires_at_least_one_question() -> None:
    with pytest.raises(ValidationError):
        ResearchPlan(
            task_id=uuid4(),
            run_id=uuid4(),
            objective="Assess a system.",
            questions=[],
        )


def test_research_plan_rejects_duplicate_question_ids() -> None:
    question_id = uuid4()

    with pytest.raises(
        ValidationError,
        match="duplicate question IDs",
    ):
        ResearchPlan(
            task_id=uuid4(),
            run_id=uuid4(),
            objective="Assess a system.",
            questions=[
                ResearchQuestion(
                    question_id=question_id,
                    question="What is it?",
                ),
                ResearchQuestion(
                    question_id=question_id,
                    question="How does it work?",
                ),
            ],
        )


def test_research_plan_rejects_duplicate_questions() -> None:
    with pytest.raises(
        ValidationError,
        match="duplicate questions",
    ):
        ResearchPlan(
            task_id=uuid4(),
            run_id=uuid4(),
            objective="Assess a system.",
            questions=[
                ResearchQuestion(question="What   is it?"),
                ResearchQuestion(question=" what is IT? "),
            ],
        )


def test_blank_question_and_rationale_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchQuestion(question="   ")

    with pytest.raises(ValidationError):
        ResearchQuestion(
            question="What is it?",
            rationale="   ",
        )
