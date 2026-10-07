from abc import ABC, abstractmethod
from uuid import UUID

from enterprise_research.domain.research_task import (
    ResearchTask,
)


class ResearchTaskRepository(ABC):

    @abstractmethod
    def save(
        self,
        task: ResearchTask,
    ) -> None:
        pass


    @abstractmethod
    def get(
        self,
        task_id: UUID,
    ) -> ResearchTask | None:
        pass
