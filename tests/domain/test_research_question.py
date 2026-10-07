from uuid import uuid4

from enterprise_research.domain.research_question import (
    QuestionStatus,
    ResearchQuestion,
)


def test_question_lifecycle():

    question = ResearchQuestion(
        task_id=uuid4(),
        question="What are the market trends?"
    )


    assert question.status == (
        QuestionStatus.PENDING
    )


    question.start()

    assert question.status == (
        QuestionStatus.RESEARCHING
    )


    question.answer()

    assert question.status == (
        QuestionStatus.ANSWERED
    )
