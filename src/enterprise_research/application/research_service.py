from enterprise_research.application.repositories import (
    ResearchTaskRepository,
)

from enterprise_research.domain.claim import Claim
from enterprise_research.domain.evidence import Evidence
from enterprise_research.domain.report import ResearchReport
from enterprise_research.domain.research_question import ResearchQuestion
from enterprise_research.domain.research_task import ResearchTask
from enterprise_research.domain.source import Source


class ResearchService:
    """
    Application service coordinating enterprise research workflow.

    Application layer is responsible for orchestrating domain objects
    and communicating with external ports such as repositories.
    """

    def __init__(
        self,
        task_repository: ResearchTaskRepository,
    ):
        self.task_repository = task_repository


    def create_task(
        self,
        title: str,
        description: str,
    ) -> ResearchTask:

        task = ResearchTask(
            title=title,
            description=description,
        )

        self.task_repository.save(task)

        return task


    def add_question(
        self,
        task: ResearchTask,
        question_text: str,
    ) -> ResearchQuestion:

        return ResearchQuestion(
            task_id=task.id,
            question=question_text,
        )


    def collect_source(
        self,
        url: str,
        title: str,
    ) -> Source:

        return Source(
            url=url,
            title=title,
        )


    def extract_evidence(
        self,
        source: Source,
        quote: str,
    ) -> Evidence:

        return Evidence(
            source_id=source.id,
            quote=quote,
        )


    def create_claim(
        self,
        statement: str,
    ) -> Claim:

        return Claim(
            statement=statement,
        )


    def create_report(
        self,
        task: ResearchTask,
        title: str,
    ) -> ResearchReport:

        return ResearchReport(
            task_id=task.id,
            title=title,
        )
