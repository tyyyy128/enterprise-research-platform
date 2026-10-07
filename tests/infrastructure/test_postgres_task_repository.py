from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_research.domain.research_task import (
    ResearchStatus,
    ResearchTask,
)

from enterprise_research.infrastructure.database.connection import (
    Base,
)

from enterprise_research.infrastructure.database.models import (
    ResearchTaskModel,
)

from enterprise_research.infrastructure.database.postgres_task_repository import (
    SQLAlchemyResearchTaskRepository,
)


def test_sqlalchemy_repository_save_and_get():

    engine = create_engine(
        "sqlite:///:memory:",
    )


    Base.metadata.create_all(
        engine
    )


    with Session(engine) as session:

        repository = (
            SQLAlchemyResearchTaskRepository(
                session
            )
        )


        task = ResearchTask(
            title="Enterprise AI Research",
            description="Analyze AI trends",
        )


        repository.save(
            task
        )


        loaded = repository.get(
            task.id
        )


        assert loaded is not None

        assert loaded.id == task.id

        assert loaded.title == (
            "Enterprise AI Research"
        )

        assert loaded.status == (
            ResearchStatus.CREATED
        )
