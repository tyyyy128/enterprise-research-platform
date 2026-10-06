"""In-memory repository for retrieved source documents."""

from __future__ import annotations

import asyncio
from uuid import UUID

from enterprise_research.domain import RetrievedDocument


class InMemoryRetrievedDocumentRepository:
    """Process-local repository used by tests and memory runtime."""

    def __init__(self) -> None:
        self._items: dict[
            UUID,
            RetrievedDocument,
        ] = {}

        self._lock = asyncio.Lock()

    async def save(
        self,
        document: RetrievedDocument,
    ) -> None:
        """Persist an isolated copy of the latest retrieval outcome."""
        async with self._lock:
            self._items[document.source_id] = document.model_copy(deep=True)

    async def get_for_source(
        self,
        source_id: UUID,
    ) -> RetrievedDocument | None:
        """Return an isolated copy of one source's retrieval outcome."""
        async with self._lock:
            document = self._items.get(source_id)

            if document is None:
                return None

            return document.model_copy(deep=True)
