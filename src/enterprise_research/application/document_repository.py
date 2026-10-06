"""Repository contract for retrieved source documents."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from enterprise_research.domain.document import (
    RetrievedDocument,
)


class RetrievedDocumentRepository(Protocol):
    """Persist the latest retrieval outcome for each source."""

    async def save(
        self,
        document: RetrievedDocument,
    ) -> None:
        """Insert or replace one source's retrieval outcome."""

    async def get_for_source(
        self,
        source_id: UUID,
    ) -> RetrievedDocument | None:
        """Return the latest retrieval outcome for one source."""
