from uuid import UUID

from enterprise_research.domain.research_task import (
    ResearchStatus,
    ResearchTask,
)

from enterprise_research.infrastructure.database.models import (
    ResearchTaskModel,
)


def task_to_model(
    task: ResearchTask,
) -> ResearchTaskModel:
    """
    Convert domain entity to database model.
    """

    return ResearchTaskModel(
        id=str(task.id),
        title=task.title,
        description=task.description,
        status=task.status.value,
        created_at=task.created_at.replace(
            tzinfo=None
        ),
    )


def model_to_task(
    model: ResearchTaskModel,
) -> ResearchTask:
    """
    Convert database model back to domain entity.
    """

    task = ResearchTask(
        title=model.title,
        description=model.description,
        id=UUID(model.id),
    )

    task.status = ResearchStatus(
        model.status
    )

    task.created_at = model.created_at

    return task
