"""PostgreSQL repository for retrieved source documents."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from enterprise_research.domain import (
    RetrievalStatus,
    RetrievedDocument,
)
from enterprise_research.infrastructure.postgres.models import (
    ResearchSourceDocumentRow,
)


class PostgresRetrievedDocumentRepository:
    """Persist the latest retrieval outcome for each source."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        document: RetrievedDocument,
    ) -> None:
        """Insert or replace one source's latest retrieval outcome."""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    ResearchSourceDocumentRow,
                    document.source_id,
                )

                values = {
                    "requested_url": str(document.requested_url),
                    "final_url": (str(document.final_url) if document.final_url is not None else None),
                    "status": (document.status.value),
                    "content": document.content,
                    "content_type": (document.content_type),
                    "content_hash": (document.content_hash),
                    "fetched_at": (document.fetched_at),
                    "error": document.error,
                    "document_metadata": dict(document.metadata),
                }

                if row is None:
                    session.add(
                        ResearchSourceDocumentRow(
                            source_id=(document.source_id),
                            **values,
                        )
                    )
                    return

                for name, value in values.items():
                    setattr(
                        row,
                        name,
                        value,
                    )

    async def get_for_source(
        self,
        source_id: UUID,
    ) -> RetrievedDocument | None:
        """Return one source's latest retrieval outcome."""
        async with self._session_factory() as session:
            row = await session.get(
                ResearchSourceDocumentRow,
                source_id,
            )

            if row is None:
                return None

            return RetrievedDocument(
                source_id=row.source_id,
                requested_url=(row.requested_url),
                final_url=row.final_url,
                status=RetrievalStatus(row.status),
                content=row.content,
                content_type=(row.content_type),
                content_hash=(row.content_hash),
                fetched_at=row.fetched_at,
                error=row.error,
                metadata=dict(row.document_metadata),
            )
