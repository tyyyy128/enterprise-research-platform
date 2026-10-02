"""Runtime service and HTTP API for Enterprise Research."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter, Depends, HTTPException, status

from enterprise_research.api import (
    CreateEvidenceRequest,
    CreateResearchTaskRequest,
    CreateSourceRequest,
    EvidenceResponse,
    ResearchTaskResponse,
    SourceResponse,
)
from enterprise_research.application import (
    ResearchApplicationService,
    ResearchTaskNotFoundError,
)
from enterprise_research.domain import (
    Evidence,
    Source,
    UnknownSourceError,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
)


class EnterpriseResearchService:
    """Application-scoped runtime service for Enterprise Research."""

    def __init__(self) -> None:
        self._deps: ExtensionRuntimeDeps | None = None
        self._application: ResearchApplicationService | None = None

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        """Start runtime resources."""
        self._deps = deps

        repository = InMemoryResearchRepository()
        self._application = ResearchApplicationService(repository)

    async def stop(self) -> None:
        """Release runtime resources."""
        self._application = None
        self._deps = None

    async def require_deps(self) -> ExtensionRuntimeDeps:
        """Return DeerFlow runtime dependencies."""
        deps = self._deps

        if deps is None or deps.app_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="enterprise-research extension is not running",
            )

        return deps

    async def require_application(
        self,
    ) -> ResearchApplicationService:
        """Return the application service while runtime is active."""
        application = self._application

        if application is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="enterprise-research extension is not running",
            )

        return application


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

    @router.post(
        "/tasks",
        response_model=ResearchTaskResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_task(
        request: CreateResearchTaskRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ResearchTaskResponse:
        aggregate = await application.create_task(request.query)

        return ResearchTaskResponse.from_aggregate(aggregate)

    @router.get(
        "/tasks/{task_id}",
        response_model=ResearchTaskResponse,
    )
    async def get_task(
        task_id: UUID,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ResearchTaskResponse:
        try:
            aggregate = await application.get_task(task_id)
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return ResearchTaskResponse.from_aggregate(aggregate)

    @router.post(
        "/tasks/{task_id}/sources",
        response_model=SourceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_source(
        task_id: UUID,
        request: CreateSourceRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> SourceResponse:
        source = Source(
            source_type=request.source_type,
            title=request.title,
            url=request.url,
            content_hash=request.content_hash,
            metadata=request.metadata,
        )

        try:
            created = await application.add_source(
                task_id,
                source,
            )
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return SourceResponse.from_domain(created)

    @router.post(
        "/tasks/{task_id}/evidence",
        response_model=EvidenceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_evidence(
        task_id: UUID,
        request: CreateEvidenceRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> EvidenceResponse:
        evidence = Evidence(
            source_id=request.source_id,
            content=request.content,
            locator=request.locator,
            relevance_score=request.relevance_score,
        )

        try:
            created = await application.add_evidence(
                task_id,
                evidence,
            )
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except UnknownSourceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return EvidenceResponse.from_domain(created)

    return router
