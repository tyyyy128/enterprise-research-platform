from uuid import UUID

from enterprise_research.domain.research_task import (
    ResearchTask,
)


class MemoryResearchTaskRepository:

    def __init__(self):
        self._items: dict[
            UUID,
            ResearchTask
        ] = {}


    def save(
        self,
        task: ResearchTask,
    ) -> None:

        self._items[task.id] = task


    def get(
        self,
        task_id: UUID,
    ) -> ResearchTask | None:

        return self._items.get(task_id)
