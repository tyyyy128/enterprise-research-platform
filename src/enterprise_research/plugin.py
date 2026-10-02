"""Runtime service and HTTP API for Enterprise Research."""

from __future__ import annotations

from typing import Any

from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter, Depends, HTTPException


class EnterpriseResearchService:
    """Application-scoped runtime service for Enterprise Research."""

    def __init__(self) -> None:
        self._deps: ExtensionRuntimeDeps | None = None

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        """Called when the DeerFlow Gateway starts the extension."""
        self._deps = deps

    async def stop(self) -> None:
        """Called when the DeerFlow Gateway stops the extension."""
        self._deps = None

    async def require_deps(self) -> ExtensionRuntimeDeps:
        """Return runtime dependencies only while the service is running."""
        deps = self._deps

        if deps is None or deps.app_store is None:
            raise HTTPException(
                status_code=503,
                detail="enterprise-research extension is not running",
            )

        return deps


def build_router(service: EnterpriseResearchService) -> APIRouter:
    """Build Enterprise Research HTTP endpoints."""

    router = APIRouter(
        prefix="/api/enterprise-research",
        tags=["enterprise-research"],
    )

    @router.get("/health")
    async def health(
        deps: ExtensionRuntimeDeps = Depends(service.require_deps),
    ) -> dict[str, Any]:
        assert deps.app_store is not None

        return {
            "status": "ok",
            "extension": "enterprise-research",
            "scope_id": deps.app_store.scope_id,
            "session_factory_available": deps.session_factory is not None,
        }

    return router
