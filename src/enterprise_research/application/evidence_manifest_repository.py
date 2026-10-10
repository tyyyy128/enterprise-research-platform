"""Persistence contract for run-scoped evidence completion receipts."""

from __future__ import annotations

from typing import Protocol

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_completion import EvidenceCompletionManifest


class ResearchEvidenceManifestRepository(Protocol):
    async def save_guarded(self, manifest: EvidenceCompletionManifest, lease: ResearchRunLease) -> None:
        """Persist a receipt only for the leased VERIFYING stage."""
