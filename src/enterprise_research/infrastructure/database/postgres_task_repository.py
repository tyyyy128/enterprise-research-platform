from uuid import UUID

from sqlalchemy.orm import Session

from enterprise_research.application.repositories import (
    ResearchTaskRepository,
)

from enterprise_research.domain.research_task import (
    ResearchTask,
)

from enterprise_research.infrastructure.database.mappers import (
    model_to_task,
    task_to_model,
)

from enterprise_research.infrastructure.database.models import (
    ResearchTaskModel,
)


class SQLAlchemyResearchTaskRepository(
    ResearchTaskRepository
):
    """
    SQLAlchemy implementation of ResearchTaskRepository.
    """

    def __init__(
        self,
        session: Session,
    ):
        self.session = session


    def save(
        self,
        task: ResearchTask,
    ) -> None:

        model = task_to_model(task)

        self.session.merge(
            model
        )

        self.session.commit()


    def get(
        self,
        task_id: UUID,
    ) -> ResearchTask | None:

        model = self.session.get(
            ResearchTaskModel,
            str(task_id),
        )

        if model is None:
            return None

        return model_to_task(
            model
        )
