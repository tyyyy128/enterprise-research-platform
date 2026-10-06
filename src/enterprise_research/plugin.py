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
    ResearchPipelineStageRunner,
    ResearchPlanningService,
    ResearchRetrievalService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchRunNotFoundError,
    ResearchSearchService,
    ResearchTaskNotFoundError,
    SkeletonResearchStageRunner,
)
from enterprise_research.application.document_chunking import (
    DocumentChunkingSettings,
)
from enterprise_research.application.evidence_extraction_service import (
    ResearchEvidenceExtractionService,
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
    InMemoryRetrievedDocumentRepository,
)
from enterprise_research.infrastructure.evidence_extraction import (
    OpenAICompatibleEvidenceExtractor,
    OpenAICompatibleEvidenceExtractorSettings,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)
from enterprise_research.infrastructure.planning import (
    OpenAICompatiblePlannerSettings,
    OpenAICompatibleResearchPlanner,
)
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresRetrievedDocumentRepository,
    PostgresSettings,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.plan_repository import (
    PostgresResearchPlanRepository,
)
from enterprise_research.infrastructure.retrieval import (
    TavilyExtractSettings,
    TavilyExtractWebContentFetcher,
)
from enterprise_research.infrastructure.searching import (
    TavilySearchProvider,
    TavilySearchSettings,
)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


class EnterpriseResearchService:
    """Application-scoped runtime service for Enterprise Research."""

    def __init__(self) -> None:
        self._deps: ExtensionRuntimeDeps | None = None
        self._application: ResearchApplicationService | None = None
        self._run_application: ResearchRunApplicationService | None = None
        self._run_executor: ResearchRunExecutor | None = None
        self._engine: AsyncEngine | None = None

    async def start(
        self,
        deps: ExtensionRuntimeDeps,
    ) -> None:
        """Start runtime resources."""
        self._deps = deps

        try:
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
                plan_repository = InMemoryResearchPlanRepository()
                document_repository = InMemoryRetrievedDocumentRepository()

            elif repository_backend == "postgres":
                (
                    research_repository,
                    run_repository,
                    plan_repository,
                ) = await self._build_postgres_repositories()
                if self._engine is None:
                    raise RuntimeError("PostgreSQL engine was not initialized.")
                document_repository = PostgresRetrievedDocumentRepository(build_session_factory(self._engine))

            else:
                raise RuntimeError(
                    "Unsupported Enterprise Research "
                    "repository: "
                    f"{repository_backend!r}. "
                    "Expected 'memory' or 'postgres'."
                )

            application = ResearchApplicationService(research_repository)

            run_application = ResearchRunApplicationService(
                research_repository,
                run_repository,
            )

            planner_backend = (
                os.getenv(
                    "ENTERPRISE_RESEARCH_PLANNER_BACKEND",
                    "disabled",
                )
                .strip()
                .lower()
            )

            search_backend = (
                os.getenv(
                    "ENTERPRISE_RESEARCH_SEARCH_BACKEND",
                    "disabled",
                )
                .strip()
                .lower()
            )

            if planner_backend == "disabled":
                if search_backend != "disabled":
                    raise RuntimeError("Enterprise Research search requires a real planner backend.")

                stage_runner = SkeletonResearchStageRunner()

            elif planner_backend == "openai-compatible":
                planner = OpenAICompatibleResearchPlanner(
                    OpenAICompatiblePlannerSettings(
                        base_url=_required_env("ENTERPRISE_RESEARCH_PLANNER_BASE_URL"),
                        api_key=_required_env("ENTERPRISE_RESEARCH_PLANNER_API_KEY"),
                        model=_required_env("ENTERPRISE_RESEARCH_PLANNER_MODEL"),
                    )
                )

                planning_service = ResearchPlanningService(
                    research_repository,
                    run_repository,
                    plan_repository,
                    planner,
                )

                if search_backend == "disabled":
                    search_service = None

                elif search_backend == "tavily":
                    raw_max_results = os.getenv(
                        "ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS",
                        "5",
                    ).strip()

                    try:
                        max_results = int(raw_max_results)
                    except ValueError as exc:
                        raise RuntimeError("ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS must be an integer.") from exc

                    if not 1 <= max_results <= 20:
                        raise RuntimeError("ENTERPRISE_RESEARCH_SEARCH_MAX_RESULTS must be between 1 and 20.")

                    search_service = ResearchSearchService(
                        research_repository,
                        run_repository,
                        plan_repository,
                        TavilySearchProvider(TavilySearchSettings(api_key=_required_env("TAVILY_API_KEY"))),
                        max_results_per_question=(max_results),
                    )

                else:
                    raise RuntimeError(
                        "Unsupported Enterprise Research "
                        "search backend: "
                        f"{search_backend!r}. "
                        "Expected 'disabled' or 'tavily'."
                    )

                retrieval_backend = (
                    os.getenv(
                        "ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND",
                        "disabled",
                    )
                    .strip()
                    .lower()
                )

                if retrieval_backend == "disabled":
                    retrieval_service = None

                elif retrieval_backend == "tavily-extract":
                    retrieval_service = ResearchRetrievalService(
                        research_repository,
                        run_repository,
                        document_repository,
                        TavilyExtractWebContentFetcher(
                            TavilyExtractSettings(
                                api_key=_required_env("TAVILY_API_KEY"),
                                base_url=(
                                    os.getenv(
                                        "ENTERPRISE_RESEARCH_RETRIEVAL_BASE_URL",
                                        "https://api.tavily.com",
                                    ).strip()
                                    or "https://api.tavily.com"
                                ),
                                extract_depth=os.getenv(
                                    "ENTERPRISE_RESEARCH_RETRIEVAL_EXTRACT_DEPTH",
                                    "basic",
                                )
                                .strip()
                                .lower(),
                                extract_timeout_seconds=float(
                                    os.getenv(
                                        "ENTERPRISE_RESEARCH_RETRIEVAL_EXTRACT_TIMEOUT_SECONDS",
                                        "30",
                                    )
                                ),
                                http_timeout_seconds=float(
                                    os.getenv(
                                        "ENTERPRISE_RESEARCH_RETRIEVAL_HTTP_TIMEOUT_SECONDS",
                                        "60",
                                    )
                                ),
                                max_content_chars=int(
                                    os.getenv(
                                        "ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS",
                                        "1000000",
                                    )
                                ),
                            )
                        ),
                    )

                else:
                    raise RuntimeError(
                        "Unsupported Enterprise Research retrieval backend: "
                        f"{retrieval_backend!r}. Expected 'disabled' or "
                        "'tavily-extract'."
                    )

                evidence_backend = (
                    os.getenv(
                        "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
                        "disabled",
                    )
                    .strip()
                    .lower()
                )

                if evidence_backend == "disabled":
                    evidence_extraction_service = None

                elif evidence_backend == "openai-compatible":
                    try:
                        evidence_temperature = float(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_TEMPERATURE",
                                "0",
                            )
                        )

                        evidence_timeout_seconds = float(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_TIMEOUT_SECONDS",
                                "60",
                            )
                        )

                        evidence_max_candidates = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CANDIDATES_PER_CHUNK",
                                "6",
                            )
                        )

                        evidence_max_excerpt_chars = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_EXCERPT_CHARS",
                                "2000",
                            )
                        )

                        evidence_chunk_size_chars = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS",
                                "20000",
                            )
                        )

                        evidence_chunk_overlap_chars = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS",
                                "1000",
                            )
                        )

                    except ValueError as exc:
                        raise RuntimeError("Enterprise Research evidence numeric settings are invalid.") from exc

                    evidence_extraction_service = ResearchEvidenceExtractionService(
                        research_repository,
                        run_repository,
                        plan_repository,
                        document_repository,
                        OpenAICompatibleEvidenceExtractor(
                            OpenAICompatibleEvidenceExtractorSettings(
                                base_url=_required_env("ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL"),
                                api_key=_required_env("ENTERPRISE_RESEARCH_EVIDENCE_API_KEY"),
                                model=_required_env("ENTERPRISE_RESEARCH_EVIDENCE_MODEL"),
                                temperature=(evidence_temperature),
                                timeout_seconds=(evidence_timeout_seconds),
                                max_candidates_per_chunk=(evidence_max_candidates),
                                max_excerpt_chars=(evidence_max_excerpt_chars),
                            )
                        ),
                        chunking_settings=(
                            DocumentChunkingSettings(
                                chunk_size_chars=(evidence_chunk_size_chars),
                                overlap_chars=(evidence_chunk_overlap_chars),
                            )
                        ),
                    )

                else:
                    raise RuntimeError(
                        "Unsupported Enterprise Research "
                        "evidence backend: "
                        f"{evidence_backend!r}. "
                        "Expected 'disabled' or "
                        "'openai-compatible'."
                    )

                stage_runner = ResearchPipelineStageRunner(
                    planning_service,
                    search_service,
                    retrieval_service=retrieval_service,
                    evidence_extraction_service=evidence_extraction_service,
                )

            else:
                raise RuntimeError(
                    "Unsupported Enterprise Research "
                    "planner backend: "
                    f"{planner_backend!r}. "
                    "Expected 'disabled' or "
                    "'openai-compatible'."
                )

            run_executor = ResearchRunExecutor(
                run_application,
                stage_runner,
            )

        except Exception:
            if self._engine is not None:
                await self._engine.dispose()
                self._engine = None

            self._application = None
            self._run_application = None
            self._run_executor = None
            self._deps = None

            raise

        self._application = application
        self._run_application = run_application
        self._run_executor = run_executor

    async def _build_postgres_repositories(
        self,
    ) -> tuple[
        PostgresResearchRepository,
        PostgresResearchRunRepository,
        PostgresResearchPlanRepository,
    ]:
        """Build and verify PostgreSQL repositories."""
        settings = PostgresSettings.from_env()
        engine = build_async_engine(settings)

        try:
            async with engine.connect() as connection:
                required_tables = (
                    "research_tasks",
                    "research_runs",
                    "research_plans",
                    "research_plan_questions",
                    "research_source_documents",
                )

                for table_name in required_tables:
                    table = await connection.scalar(text(f"SELECT to_regclass('public.{table_name}')"))

                    if table is None:
                        raise RuntimeError(
                            "Enterprise Research "
                            "PostgreSQL schema is not "
                            "initialized. Run "
                            "'alembic upgrade head' first."
                        )

        except Exception:
            await engine.dispose()
            raise

        self._engine = engine

        session_factory = build_session_factory(engine)

        return (
            PostgresResearchRepository(session_factory),
            PostgresResearchRunRepository(session_factory),
            PostgresResearchPlanRepository(session_factory),
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
