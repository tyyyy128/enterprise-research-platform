from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_research.infrastructure.database.connection import (
    Base,
)

from enterprise_research.infrastructure.database.models import (
    ResearchTaskModel,
)


def test_research_task_table_creation():

    engine = create_engine(
        "sqlite:///:memory:",
    )


    Base.metadata.create_all(
        engine
    )


    with Session(engine) as session:

        task = ResearchTaskModel(
            id="task-1",
            title="AI Research",
            description="Analyze AI trends",
            status="created",
        )

        session.add(task)

        session.commit()


        result = session.get(
            ResearchTaskModel,
            "task-1",
        )


        assert result is not None

        assert result.title == (
            "AI Research"
        )
