"""Runtime service and HTTP API for Enterprise Research."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from enterprise_research.api import (
    ClaimLineageResponse,
    ClaimResponse,
    CreateClaimRequest,
    CreateEvidenceRequest,
    CreateReportRequest,
    CreateResearchTaskRequest,
    CreateSourceRequest,
    EvidenceResponse,
    ResearchReportResponse,
    ResearchRunResponse,
    ResearchTaskResponse,
    SourceResponse,
    SupportClaimRequest,
)
from enterprise_research.application import (
    ResearchApplicationService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchRunNotFoundError,
    ResearchTaskNotFoundError,
    SkeletonResearchStageRunner,
)
from enterprise_research.domain import (
    Claim,
    Evidence,
    InvalidReportError,
    Source,
    UnknownClaimError,
    UnknownEvidenceError,
    UnknownSourceError,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    build_async_engine,
    build_session_factory,
)


class EnterpriseResearchService:
    """Application-scoped runtime service for Enterprise Research."""

    def __init__(self) -> None:
        self._deps: ExtensionRuntimeDeps | None = None
        self._application: ResearchApplicationService | None = None
        self._run_application: ResearchRunApplicationService | None = None
        self._run_executor: ResearchRunExecutor | None = None
        self._engine: AsyncEngine | None = None

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        """Start runtime resources."""
        self._deps = deps

        repository_backend = (
            os.getenv(
                "ENTERPRISE_RESEARCH_REPOSITORY",
                "memory",
            )
            .strip()
            .lower()
        )

        if repository_backend == "memory":
            research_repository = InMemoryResearchRepository()
            run_repository = InMemoryResearchRunRepository()

        elif repository_backend == "postgres":
            try:
                (
                    research_repository,
                    run_repository,
                ) = await self._build_postgres_repositories()
            except Exception:
                self._deps = None
                raise

        else:
            self._deps = None

            raise RuntimeError(
                f"Unsupported Enterprise Research repository: {repository_backend!r}. Expected 'memory' or 'postgres'."
            )

        self._application = ResearchApplicationService(research_repository)
        self._run_application = ResearchRunApplicationService(
            research_repository,
            run_repository,
        )
        self._run_executor = ResearchRunExecutor(
            self._run_application,
            SkeletonResearchStageRunner(),
        )

    async def _build_postgres_repositories(
        self,
    ) -> tuple[
        PostgresResearchRepository,
        PostgresResearchRunRepository,
    ]:
        """Build and verify PostgreSQL repositories."""
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)

        try:
            async with engine.connect() as connection:
                research_tasks_table = await connection.scalar(
                    text(
                        """
                        SELECT to_regclass(
                            'public.research_tasks'
                        )
                        """
                    )
                )
                research_runs_table = await connection.scalar(
                    text(
                        """
                        SELECT to_regclass(
                            'public.research_runs'
                        )
                        """
                    )
                )

                if research_tasks_table is None or research_runs_table is None:
                    raise RuntimeError(
                        "Enterprise Research PostgreSQL schema is not initialized. Run 'alembic upgrade head' first."
                    )
        except Exception:
            await engine.dispose()
            raise

        self._engine = engine
        session_factory = build_session_factory(engine)

        return (
            PostgresResearchRepository(session_factory),
            PostgresResearchRunRepository(session_factory),
        )

    async def stop(self) -> None:
        """Release runtime resources."""
        if self._run_executor is not None:
            await self._run_executor.shutdown()
            self._run_executor = None

        self._application = None
        self._run_application = None

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

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

    async def require_run_application(
        self,
    ) -> ResearchRunApplicationService:
        """Return the run application service while runtime is active."""
        application = self._run_application

        if application is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="enterprise-research extension is not running",
            )

        return application

    async def require_run_executor(
        self,
    ) -> ResearchRunExecutor:
        """Return the active research run executor."""
        executor = self._run_executor

        if executor is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="enterprise-research extension is not running",
            )

        return executor


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

    @router.post(
        "/tasks/{task_id}/claims",
        response_model=ClaimResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_claim(
        task_id: UUID,
        request: CreateClaimRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ClaimResponse:
        claim = Claim(
            text=request.text,
            evidence_ids=request.evidence_ids,
        )

        try:
            created = await application.add_claim(
                task_id,
                claim,
            )
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except UnknownEvidenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return ClaimResponse.from_domain(created)

    @router.post(
        "/tasks/{task_id}/claims/{claim_id}/support",
        response_model=ClaimResponse,
    )
    async def support_claim(
        task_id: UUID,
        claim_id: UUID,
        request: SupportClaimRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ClaimResponse:
        try:
            claim = await application.support_claim(
                task_id,
                claim_id,
                request.evidence_ids,
                confidence=request.confidence,
            )
        except (
            ResearchTaskNotFoundError,
            UnknownClaimError,
            UnknownEvidenceError,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return ClaimResponse.from_domain(claim)

    @router.get(
        "/tasks/{task_id}/claims/{claim_id}/lineage",
        response_model=ClaimLineageResponse,
    )
    async def trace_claim(
        task_id: UUID,
        claim_id: UUID,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ClaimLineageResponse:
        try:
            lineage = await application.trace_claim(
                task_id,
                claim_id,
            )
        except (
            ResearchTaskNotFoundError,
            UnknownClaimError,
            UnknownEvidenceError,
            UnknownSourceError,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return ClaimLineageResponse.from_domain(
            claim_id,
            lineage,
        )

    @router.post(
        "/tasks/{task_id}/report",
        response_model=ResearchReportResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def build_report(
        task_id: UUID,
        request: CreateReportRequest,
        application: ResearchApplicationService = Depends(service.require_application),
    ) -> ResearchReportResponse:
        try:
            report = await application.build_report(
                task_id,
                title=request.title,
                summary=request.summary,
                claim_ids=request.claim_ids,
            )
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except UnknownClaimError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except InvalidReportError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        return ResearchReportResponse.from_domain(report)

    @router.post(
        "/tasks/{task_id}/runs",
        response_model=ResearchRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_run(
        task_id: UUID,
        application: ResearchRunApplicationService = Depends(service.require_run_application),
        executor: ResearchRunExecutor = Depends(service.require_run_executor),
    ) -> ResearchRunResponse:
        try:
            run = await application.create_run(task_id)
        except ResearchTaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        executor.start(
            task_id,
            run.run_id,
        )

        return ResearchRunResponse.from_domain(run)

    @router.get(
        "/tasks/{task_id}/runs/{run_id}",
        response_model=ResearchRunResponse,
    )
    async def get_run(
        task_id: UUID,
        run_id: UUID,
        application: ResearchRunApplicationService = Depends(service.require_run_application),
    ) -> ResearchRunResponse:
        try:
            run = await application.get_run(
                task_id,
                run_id,
            )
        except ResearchRunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        return ResearchRunResponse.from_domain(run)

    return router
