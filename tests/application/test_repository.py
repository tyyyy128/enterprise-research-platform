from enterprise_research.domain.research_task import (
    ResearchTask,
)

from enterprise_research.infrastructure.memory_task_repository import (
    MemoryResearchTaskRepository,
)


def test_task_repository():

    repo = MemoryResearchTaskRepository()


    task = ResearchTask(
        title="AI Research",
        description="Analyze AI market",
    )


    repo.save(task)


    loaded = repo.get(
        task.id
    )


    assert loaded == task
