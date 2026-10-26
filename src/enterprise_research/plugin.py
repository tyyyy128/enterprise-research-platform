"""Runtime service and HTTP API for Enterprise Research."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID

from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
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
    ResearchRunReportResponse,
    ResearchRunResponse,
    ResearchTaskResponse,
    SourceResponse,
    SupportClaimRequest,
)
from enterprise_research.api.sse import encode_run_event
from enterprise_research.application import (
    ResearchApplicationService,
    ResearchLangGraphStageRunner,
    ResearchPipelineStageRunner,
    ResearchPlanningService,
    ResearchRetrievalService,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchRunNotFoundError,
    ResearchSafeResumeInspector,
    ResearchSearchService,
    ResearchTaskNotFoundError,
    SkeletonResearchStageRunner,
)
from enterprise_research.application.claim_generation import ClaimGenerationLimits, ResearchClaimGenerationService
from enterprise_research.application.claim_verification import (
    ClaimVerificationLimits,
    ResearchClaimVerificationService,
)
from enterprise_research.application.document_chunking import (
    DocumentChunkingSettings,
)
from enterprise_research.application.evidence_extraction_service import (
    ResearchEvidenceExtractionService,
)
from enterprise_research.application.evidence_workload import (
    EvidenceWorkloadSettings,
)
from enterprise_research.application.financial_evidence_scoring import target_financial_year
from enterprise_research.application.financial_source_discovery import FinancialSourceDiscoverySettings
from enterprise_research.application.graph_checkpoint import LangGraphCheckpointReader
from enterprise_research.application.report_synthesis import ResearchReportSynthesisService
from enterprise_research.application.report_writing import ResearchReportWritingService
from enterprise_research.application.run_events import (
    ResearchRunEventBus,
    RunEventHistoryExpired,
)
from enterprise_research.domain import (
    Claim,
    Evidence,
    InvalidReportError,
    ResearchRunStatus,
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
from enterprise_research.infrastructure.claim_generation import (
    OpenAICompatibleClaimGenerator,
    OpenAICompatibleClaimGeneratorSettings,
)
from enterprise_research.infrastructure.claim_verification import (
    OpenAICompatibleClaimVerifier,
    OpenAICompatibleClaimVerifierSettings,
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
from enterprise_research.infrastructure.postgres.claim_generation import PostgresResearchClaimGenerationRepository
from enterprise_research.infrastructure.postgres.claim_verification import PostgresResearchClaimVerificationRepository
from enterprise_research.infrastructure.postgres.evidence_manifest_repository import (
    PostgresResearchEvidenceManifestRepository,
)
from enterprise_research.infrastructure.postgres.models import Base
from enterprise_research.infrastructure.postgres.plan_repository import (
    PostgresResearchPlanRepository,
)
from enterprise_research.infrastructure.postgres.report_artifacts import PostgresResearchRunReportRepository
from enterprise_research.infrastructure.report_synthesis import (
    OpenAICompatibleReportSynthesisPlanner,
    OpenAICompatibleReportSynthesisSettings,
)
from enterprise_research.infrastructure.retrieval import (
    TavilyExtractSettings,
    TavilyExtractWebContentFetcher,
)
from enterprise_research.infrastructure.searching import (
    TavilySearchProvider,
    TavilySearchSettings,
)

logger = logging.getLogger(__name__)


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
        self._run_event_bus: ResearchRunEventBus | None = None
        self._report_repository: PostgresResearchRunReportRepository | None = None
        self._engine: AsyncEngine | None = None
        self._graph_checkpoint_stack: AsyncExitStack | None = None

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
                evidence_manifest_repository = None
                report_repository = None
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
                evidence_manifest_repository = PostgresResearchEvidenceManifestRepository(
                    build_session_factory(self._engine)
                )
                report_repository = PostgresResearchRunReportRepository(build_session_factory(self._engine))

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

            # PostgreSQL survives process restarts. Inventory unfinished runs
            # but never replay them here: ownership and stage effects must
            # first be checked by a separate recovery worker.
            if repository_backend == "postgres":
                candidates = await run_application.inspect_unfinished_runs()
                if candidates:
                    logger.warning(
                        "Found %d persisted unfinished research runs at startup "
                        "(up to 100 inspected); no automatic replay was attempted",
                        len(candidates),
                    )

            run_event_bus = ResearchRunEventBus()

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

            claim_backend = os.getenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "disabled").strip().lower()
            if claim_backend not in {"disabled", "openai-compatible"}:
                raise RuntimeError("ENTERPRISE_RESEARCH_CLAIM_BACKEND must be 'disabled' or 'openai-compatible'.")
            claim_verification_backend = (
                os.getenv("ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND", "disabled").strip().lower()
            )
            if claim_verification_backend not in {"disabled", "openai-compatible"}:
                raise RuntimeError(
                    "ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND must be 'disabled' or 'openai-compatible'."
                )
            if claim_verification_backend != "disabled" and claim_backend != "openai-compatible":
                raise RuntimeError("Claim verification requires enabled 3F Claim generation.")
            writer_backend = os.getenv("ENTERPRISE_RESEARCH_WRITER_BACKEND", "disabled").strip().lower()
            if writer_backend not in {"disabled", "verified-claims", "openai-compatible"}:
                raise RuntimeError(
                    "ENTERPRISE_RESEARCH_WRITER_BACKEND must be 'disabled', 'verified-claims' or 'openai-compatible'."
                )
            if writer_backend == "openai-compatible" and (
                repository_backend != "postgres"
                or planner_backend != "openai-compatible"
                or claim_verification_backend != "openai-compatible"
            ):
                raise RuntimeError(
                    "Report synthesis requires PostgreSQL, a real planner and enabled 3G Claim verification."
                )

            if planner_backend == "disabled":
                if claim_backend != "disabled":
                    raise RuntimeError("Claim generation requires a real planner and evidence extraction runtime.")
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

                    financial_discovery = None
                    if os.getenv("ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE", "uuid").strip().lower() == (
                        "financial_precision"
                    ):
                        focus_terms = tuple(
                            term.strip()
                            for term in os.getenv("ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS", "").split(",")
                            if term.strip()
                        )
                        preferred_domains = tuple(
                            domain.strip().lower()
                            for domain in os.getenv("ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS", "").split(",")
                            if domain.strip()
                        )
                        try:
                            # Reuse the evidence selector's validation and opt-in vocabulary.
                            EvidenceWorkloadSettings(
                                selection_mode="financial_precision",
                                focus_terms=focus_terms,
                                preferred_domains=preferred_domains,
                            )
                            year = target_financial_year(focus_terms)
                            assert year is not None
                            financial_discovery = FinancialSourceDiscoverySettings(
                                target_year=year,
                                preferred_domains=preferred_domains,
                            )
                        except ValueError as exc:
                            raise RuntimeError("Financial source discovery settings are invalid.") from exc

                    search_service = ResearchSearchService(
                        research_repository,
                        run_repository,
                        plan_repository,
                        TavilySearchProvider(TavilySearchSettings(api_key=_required_env("TAVILY_API_KEY"))),
                        max_results_per_question=(max_results),
                        financial_discovery=financial_discovery,
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
                                oversize_policy=os.getenv(
                                    "ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY",
                                    "reject",
                                )
                                .strip()
                                .lower(),
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

                        evidence_max_documents = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_DOCUMENTS",
                                "4",
                            )
                        )

                        evidence_max_chunks_per_document = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CHUNKS_PER_DOCUMENT",
                                "8",
                            )
                        )

                        evidence_max_total_chunks = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS",
                                "24",
                            )
                        )

                        evidence_max_concurrency = int(
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY",
                                "4",
                            )
                        )

                        if evidence_max_concurrency < 1:
                            raise ValueError("evidence max concurrency must be at least 1.")

                        evidence_selection_mode = (
                            os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE",
                                "uuid",
                            )
                            .strip()
                            .lower()
                        )
                        evidence_focus_terms = tuple(
                            term.strip()
                            for term in os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS",
                                "",
                            ).split(",")
                            if term.strip()
                        )
                        evidence_preferred_domains = tuple(
                            domain.strip().lower()
                            for domain in os.getenv(
                                "ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS",
                                "",
                            ).split(",")
                            if domain.strip()
                        )
                        evidence_workload_settings = EvidenceWorkloadSettings(
                            max_documents=evidence_max_documents,
                            max_chunks_per_document=evidence_max_chunks_per_document,
                            max_total_chunks=evidence_max_total_chunks,
                            selection_mode=evidence_selection_mode,
                            focus_terms=evidence_focus_terms,
                            preferred_domains=evidence_preferred_domains,
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
                        workload_settings=evidence_workload_settings,
                        max_concurrency=evidence_max_concurrency,
                        manifest_repository=evidence_manifest_repository,
                    )

                else:
                    raise RuntimeError(
                        "Unsupported Enterprise Research "
                        "evidence backend: "
                        f"{evidence_backend!r}. "
                        "Expected 'disabled' or "
                        "'openai-compatible'."
                    )

                claim_generation_service = None
                if claim_backend == "openai-compatible":
                    if repository_backend != "postgres" or evidence_extraction_service is None or self._engine is None:
                        raise RuntimeError("Claim generation requires PostgreSQL and a real evidence extractor.")
                    try:
                        claim_limits = ClaimGenerationLimits(
                            max_evidence=int(os.getenv("ENTERPRISE_RESEARCH_CLAIM_MAX_EVIDENCE", "12")),
                            max_excerpt_chars=int(os.getenv("ENTERPRISE_RESEARCH_CLAIM_MAX_EXCERPT_CHARS", "1800")),
                            max_candidates=int(os.getenv("ENTERPRISE_RESEARCH_CLAIM_MAX_CANDIDATES", "12")),
                            max_claim_chars=int(os.getenv("ENTERPRISE_RESEARCH_CLAIM_MAX_CLAIM_CHARS", "600")),
                            max_references_per_claim=int(
                                os.getenv("ENTERPRISE_RESEARCH_CLAIM_MAX_REFERENCES_PER_CLAIM", "5")
                            ),
                        )
                        claim_timeout = float(os.getenv("ENTERPRISE_RESEARCH_CLAIM_TIMEOUT_SECONDS", "60"))
                    except ValueError as exc:
                        raise RuntimeError("Enterprise Research claim generation limits are invalid.") from exc
                    claim_model = _required_env("ENTERPRISE_RESEARCH_CLAIM_MODEL")
                    claim_generation_service = ResearchClaimGenerationService(
                        PostgresResearchClaimGenerationRepository(build_session_factory(self._engine)),
                        OpenAICompatibleClaimGenerator(
                            OpenAICompatibleClaimGeneratorSettings(
                                base_url=_required_env("ENTERPRISE_RESEARCH_CLAIM_BASE_URL"),
                                api_key=_required_env("ENTERPRISE_RESEARCH_CLAIM_API_KEY"),
                                model=claim_model,
                                limits=claim_limits,
                                timeout_seconds=claim_timeout,
                            )
                        ),
                        model=claim_model,
                        limits=claim_limits,
                    )

                claim_verification_service = None
                if claim_verification_backend == "openai-compatible":
                    if claim_generation_service is None or self._engine is None:
                        raise RuntimeError("Claim verification requires PostgreSQL and enabled Claim generation.")
                    try:
                        verification_limits = ClaimVerificationLimits(
                            max_claims=int(os.getenv("ENTERPRISE_RESEARCH_VERIFY_MAX_CLAIMS", "12")),
                            max_evidence=int(os.getenv("ENTERPRISE_RESEARCH_VERIFY_MAX_EVIDENCE", "12")),
                            max_excerpt_chars=int(os.getenv("ENTERPRISE_RESEARCH_VERIFY_MAX_EXCERPT_CHARS", "1800")),
                            max_quote_chars=int(os.getenv("ENTERPRISE_RESEARCH_VERIFY_MAX_QUOTE_CHARS", "320")),
                            max_rationale_chars=int(
                                os.getenv("ENTERPRISE_RESEARCH_VERIFY_MAX_RATIONALE_CHARS", "1000")
                            ),
                            min_supported_confidence=float(
                                os.getenv("ENTERPRISE_RESEARCH_VERIFY_MIN_SUPPORTED_CONFIDENCE", "0.8")
                            ),
                        )
                        verification_timeout = float(os.getenv("ENTERPRISE_RESEARCH_VERIFY_TIMEOUT_SECONDS", "60"))
                    except ValueError as exc:
                        raise RuntimeError("Enterprise Research verification limits are invalid.") from exc
                    if verification_limits.max_claims < claim_limits.max_candidates:
                        raise RuntimeError("Claim verifier max claims must cover the generation candidate budget.")
                    if verification_limits.max_evidence < claim_limits.max_evidence:
                        raise RuntimeError("Claim verifier max evidence must cover the generation evidence budget.")
                    verification_model = _required_env("ENTERPRISE_RESEARCH_VERIFY_MODEL")
                    claim_verification_service = ResearchClaimVerificationService(
                        PostgresResearchClaimVerificationRepository(build_session_factory(self._engine)),
                        OpenAICompatibleClaimVerifier(
                            OpenAICompatibleClaimVerifierSettings(
                                base_url=_required_env("ENTERPRISE_RESEARCH_VERIFY_BASE_URL"),
                                api_key=_required_env("ENTERPRISE_RESEARCH_VERIFY_API_KEY"),
                                model=verification_model,
                                limits=verification_limits,
                                timeout_seconds=verification_timeout,
                            )
                        ),
                        model=verification_model,
                        limits=verification_limits,
                    )

                if writer_backend == "disabled":
                    report_writing_service = None
                elif writer_backend == "verified-claims":
                    if report_repository is None or evidence_extraction_service is None:
                        raise RuntimeError("Verified-claims writer requires PostgreSQL and a real evidence extractor.")
                    report_writing_service = ResearchReportWritingService(report_repository)
                elif writer_backend == "openai-compatible":
                    if report_repository is None or claim_verification_service is None:
                        raise RuntimeError("Report synthesis requires PostgreSQL and enabled 3G Claim verification.")
                    try:
                        synthesis_timeout = float(os.getenv("ENTERPRISE_RESEARCH_SYNTHESIS_TIMEOUT_SECONDS", "60"))
                        synthesis_temperature = float(os.getenv("ENTERPRISE_RESEARCH_SYNTHESIS_TEMPERATURE", "0"))
                    except ValueError as exc:
                        raise RuntimeError("Enterprise Research report synthesis settings are invalid.") from exc
                    synthesis_model = _required_env("ENTERPRISE_RESEARCH_SYNTHESIS_MODEL")
                    report_writing_service = ResearchReportSynthesisService(
                        report_repository,
                        OpenAICompatibleReportSynthesisPlanner(
                            OpenAICompatibleReportSynthesisSettings(
                                base_url=_required_env("ENTERPRISE_RESEARCH_SYNTHESIS_BASE_URL"),
                                api_key=_required_env("ENTERPRISE_RESEARCH_SYNTHESIS_API_KEY"),
                                model=synthesis_model,
                                timeout_seconds=synthesis_timeout,
                                temperature=synthesis_temperature,
                                response_format=os.getenv("ENTERPRISE_RESEARCH_SYNTHESIS_RESPONSE_FORMAT", "text"),
                            )
                        ),
                        model=synthesis_model,
                    )
                else:
                    raise RuntimeError(
                        "ENTERPRISE_RESEARCH_WRITER_BACKEND must be 'disabled', "
                        "'verified-claims' or 'openai-compatible'."
                    )

                stage_runner = ResearchPipelineStageRunner(
                    planning_service,
                    search_service,
                    retrieval_service=retrieval_service,
                    evidence_extraction_service=evidence_extraction_service,
                    evidence_progress_callback=run_event_bus.publish_evidence_progress,
                    report_writing_service=report_writing_service,
                    claim_generation_service=claim_generation_service,
                    claim_verification_service=claim_verification_service,
                )

            else:
                raise RuntimeError(
                    "Unsupported Enterprise Research "
                    "planner backend: "
                    f"{planner_backend!r}. "
                    "Expected 'disabled' or "
                    "'openai-compatible'."
                )

            # DeerFlow does NOT expose the host saver through ExtensionRuntimeDeps.
            # Keep research checkpoints extension-owned and opt-in; do not
            # touch the host Agent's checkpoint namespace or lifecycle.
            graph_checkpoints = None
            graph_backend = os.getenv("ENTERPRISE_RESEARCH_GRAPH_BACKEND", "disabled").strip().lower()
            if graph_backend == "postgres":
                if repository_backend != "postgres":
                    raise RuntimeError("LangGraph PostgreSQL stages require ENTERPRISE_RESEARCH_REPOSITORY=postgres.")
                if planner_backend == "disabled":
                    raise RuntimeError("LangGraph stage execution requires an enabled research planner backend.")
                dsn = _required_env("ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_DSN")
                try:
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                except ImportError as exc:
                    raise RuntimeError("Install the Enterprise Research 'langgraph' optional dependencies.") from exc

                stack = AsyncExitStack()
                self._graph_checkpoint_stack = stack
                saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
                await saver.setup()
                graph_checkpoints = LangGraphCheckpointReader(saver)
                stage_runner = ResearchLangGraphStageRunner(stage_runner, saver)
            elif graph_backend != "disabled":
                raise RuntimeError("ENTERPRISE_RESEARCH_GRAPH_BACKEND must be 'disabled' or 'postgres'.")

            run_executor = ResearchRunExecutor(
                run_application,
                stage_runner,
                event_bus=run_event_bus,
                enforce_artifact_proof=(repository_backend == "postgres"),
                resume_inspector=ResearchSafeResumeInspector(
                    run_application,
                    research_repository,
                    plan_repository,
                    document_repository,
                    graph_checkpoints=graph_checkpoints,
                ),
            )

        except Exception:
            if self._graph_checkpoint_stack is not None:
                await self._graph_checkpoint_stack.aclose()
                self._graph_checkpoint_stack = None
            if self._engine is not None:
                await self._engine.dispose()
                self._engine = None

            self._application = None
            self._run_application = None
            self._run_executor = None
            self._run_event_bus = None
            self._report_repository = None
            self._deps = None

            raise

        self._application = application
        self._run_application = run_application
        self._run_executor = run_executor
        self._run_event_bus = run_event_bus
        self._report_repository = report_repository

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
                # Keep the startup gate aligned with every mapped research
                # artifact, including the later 3F/3G/3H run receipts. A
                # partially migrated database must fail BEFORE accepting a Run.
                for table_name in sorted(Base.metadata.tables):
                    table = await connection.scalar(
                        text("SELECT to_regclass(:table_name)"),
                        {"table_name": f"public.{table_name}"},
                    )
                    if table is None:
                        raise RuntimeError(
                            f"Enterprise Research PostgreSQL schema is missing {table_name!r}; "
                            "run 'alembic upgrade head' before starting the service."
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
        self._run_event_bus = None
        self._report_repository = None

        if self._graph_checkpoint_stack is not None:
            await self._graph_checkpoint_stack.aclose()
            self._graph_checkpoint_stack = None

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

    async def require_report_repository(self) -> PostgresResearchRunReportRepository:
        """Only PostgreSQL exposes the run-specific report artifact."""
        if self._report_repository is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="run report artifacts require the PostgreSQL repository",
            )
        return self._report_repository

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

    async def require_run_event_bus(self) -> ResearchRunEventBus:
        """Return the active worker-local event history."""
        event_bus = self._run_event_bus
        if event_bus is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="enterprise-research extension is not running",
            )
        return event_bus


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

    @router.get(
        "/tasks/{task_id}/runs/{run_id}/report",
        response_model=ResearchRunReportResponse,
    )
    async def get_run_report(
        task_id: UUID,
        run_id: UUID,
        application: ResearchRunApplicationService = Depends(service.require_run_application),
        repository: PostgresResearchRunReportRepository = Depends(service.require_report_repository),
    ) -> ResearchRunReportResponse:
        """Expose a run report only after the terminal PostgreSQL proof succeeds."""
        try:
            run = await application.get_run(task_id, run_id)
        except ResearchRunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if run.status != ResearchRunStatus.COMPLETED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="research run has not completed")
        artifact = await repository.get(task_id, run_id)
        if artifact is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="completed run report artifact is missing")
        return ResearchRunReportResponse.from_artifact(artifact)

    @router.get("/tasks/{task_id}/runs/{run_id}/events")
    async def stream_run_events(
        task_id: UUID,
        run_id: UUID,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        application: ResearchRunApplicationService = Depends(service.require_run_application),
        event_bus: ResearchRunEventBus = Depends(service.require_run_event_bus),
    ) -> StreamingResponse:
        """Replay and follow bounded, process-local run events as SSE."""
        try:
            await application.get_run(task_id, run_id)
        except ResearchRunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        cursor: int | None = None
        if last_event_id is not None:
            if len(last_event_id) > 20 or not last_event_id.isascii() or not last_event_id.isdecimal():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be a non-negative decimal integer.",
                )
            cursor = int(last_event_id)

        try:
            event_bus.assert_available(task_id, run_id, cursor)
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except RunEventHistoryExpired as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        async def generate():
            async for event in event_bus.stream(task_id, run_id, last_event_id=cursor):
                yield encode_run_event(event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return router
