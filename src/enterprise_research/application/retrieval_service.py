"""Application service for retrieving research source documents."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from enterprise_research.application.document_repository import (
    RetrievedDocumentRepository,
)
from enterprise_research.application.errors import (
    ResearchRetrievalError,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.retrieval import (
    WebContentFetcher,
)
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.domain import (
    ResearchRunStatus,
    RetrievalStatus,
    RetrievedDocument,
    SourceType,
)


@dataclass(frozen=True, slots=True)
class RetrievalSummary:
    """Summary of one run's source retrieval work."""

    eligible_count: int
    attempted_count: int
    succeeded_count: int
    failed_count: int
    reused_count: int

    @property
    def usable_count(self) -> int:
        """Return newly fetched plus safely reused documents."""
        return self.succeeded_count + self.reused_count

    @property
    def coverage_ratio(self) -> float:
        """Return usable documents divided by eligible sources."""
        if self.eligible_count == 0:
            return 0.0

        return self.usable_count / self.eligible_count


class ResearchRetrievalService:
    """Fetch web sources and persist per-source retrieval outcomes."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
        document_repository: RetrievedDocumentRepository,
        fetcher: WebContentFetcher,
    ) -> None:
        self._research_repository = research_repository

        self._run_repository = run_repository

        self._document_repository = document_repository

        self._fetcher = fetcher

    async def retrieve_run(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> RetrievalSummary:
        """Retrieve all eligible web sources for one research run."""
        aggregate = await self._research_repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = await self._run_repository.get(run_id)

        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")

        if run.status != ResearchRunStatus.EXTRACTING:
            raise ResearchRetrievalError("Research run must be in 'extracting' state before retrieval.")

        sources = [
            source
            for source in aggregate.sources.values()
            if (source.source_type == SourceType.WEB and source.url is not None)
        ]

        if not sources:
            raise ResearchRetrievalError("Research task has no retrievable web sources.")

        attempted_count = 0
        succeeded_count = 0
        failed_count = 0
        reused_count = 0

        for source in sources:
            requested_url = str(source.url)

            existing = await self._document_repository.get_for_source(source.source_id)

            if (
                existing is not None
                and existing.status == RetrievalStatus.SUCCEEDED
                and str(existing.requested_url) == requested_url
            ):
                reused_count += 1
                continue

            attempted_count += 1

            try:
                fetched = await self._fetcher.fetch(requested_url)

            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__

                document = RetrievedDocument.failed(
                    source_id=(source.source_id),
                    requested_url=(requested_url),
                    error=message,
                    metadata={
                        "retrieval_run_id": (str(run_id)),
                        "error_type": (exc.__class__.__name__),
                    },
                )

                await self._document_repository.save(document)

                failed_count += 1
                continue

            metadata = dict(fetched.metadata)

            metadata["retrieval_run_id"] = str(run_id)

            document = RetrievedDocument.succeeded(
                source_id=(source.source_id),
                requested_url=(requested_url),
                final_url=(fetched.final_url),
                content=(fetched.content),
                content_type=(fetched.content_type),
                metadata=metadata,
            )

            await self._document_repository.save(document)

            succeeded_count += 1

        summary = RetrievalSummary(
            eligible_count=len(sources),
            attempted_count=(attempted_count),
            succeeded_count=(succeeded_count),
            failed_count=(failed_count),
            reused_count=(reused_count),
        )

        if summary.usable_count == 0:
            raise ResearchRetrievalError("Retrieval produced no usable documents.")

        return summary
