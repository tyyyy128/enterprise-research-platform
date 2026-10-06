from __future__ import annotations

import asyncio

import pytest
from deerflow_extension_api import (
    ExtensionData,
    ExtensionRuntimeDeps,
    HostPolicySnapshot,
)

import enterprise_research.plugin as plugin_module
from enterprise_research.plugin import (
    EnterpriseResearchService,
)


def create_runtime_deps() -> ExtensionRuntimeDeps:
    return ExtensionRuntimeDeps(
        app_store=ExtensionData("app"),
        policy=HostPolicySnapshot(max_subagents_per_run=3),
        session_factory=object(),
    )


def configure_real_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_REPOSITORY",
        "memory",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_PLANNER_BACKEND",
        "openai-compatible",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_PLANNER_BASE_URL",
        "https://example.test/v1",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_PLANNER_API_KEY",
        "planner-secret",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_PLANNER_MODEL",
        "test-planner",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_SEARCH_BACKEND",
        "disabled",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND",
        "disabled",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_TEMPERATURE",
        "0",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_TIMEOUT_SECONDS",
        "60",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CANDIDATES_PER_CHUNK",
        "6",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_EXCERPT_CHARS",
        "2000",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS",
        "20000",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS",
        "1000",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_DOCUMENTS",
        "4",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CHUNKS_PER_DOCUMENT",
        "8",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS",
        "24",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY",
        "4",
    )


def test_runtime_accepts_openai_compatible_evidence_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        configure_real_planner(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
            "openai-compatible",
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL",
            "https://example.test/v1",
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_API_KEY",
            "evidence-secret",
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MODEL",
            "test-evidence",
        )

        service = EnterpriseResearchService()

        await service.start(create_runtime_deps())

        try:
            assert await service.require_application() is not None
        finally:
            await service.stop()

    asyncio.run(run())


def test_runtime_rejects_unknown_evidence_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        configure_real_planner(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
            "mystery-provider",
        )

        service = EnterpriseResearchService()

        with pytest.raises(
            RuntimeError,
            match=("Unsupported Enterprise Research evidence backend"),
        ):
            await service.start(create_runtime_deps())

    asyncio.run(run())


def test_runtime_requires_evidence_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        configure_real_planner(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
            "openai-compatible",
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL",
            "https://example.test/v1",
        )

        monkeypatch.delenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_API_KEY",
            raising=False,
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MODEL",
            "test-evidence",
        )

        service = EnterpriseResearchService()

        with pytest.raises(
            RuntimeError,
            match=("ENTERPRISE_RESEARCH_EVIDENCE_API_KEY"),
        ):
            await service.start(create_runtime_deps())

    asyncio.run(run())


def test_runtime_applies_evidence_workload_budget_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        configure_real_planner(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
            "openai-compatible",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL",
            "https://example.test/v1",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_API_KEY",
            "evidence-secret",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MODEL",
            "test-evidence",
        )

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_DOCUMENTS",
            "2",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CHUNKS_PER_DOCUMENT",
            "3",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS",
            "5",
        )

        captured: dict[str, int] = {}
        sentinel = object()

        def build_settings(
            *,
            max_documents: int,
            max_chunks_per_document: int,
            max_total_chunks: int,
        ) -> object:
            captured.update(
                max_documents=(max_documents),
                max_chunks_per_document=(max_chunks_per_document),
                max_total_chunks=(max_total_chunks),
            )

            return sentinel

        monkeypatch.setattr(
            plugin_module,
            "EvidenceWorkloadSettings",
            build_settings,
        )

        service = EnterpriseResearchService()

        await service.start(create_runtime_deps())

        try:
            assert captured == {
                "max_documents": 2,
                "max_chunks_per_document": 3,
                "max_total_chunks": 5,
            }

        finally:
            await service.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_DOCUMENTS",
            "0",
        ),
        (
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CHUNKS_PER_DOCUMENT",
            "-1",
        ),
        (
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_TOTAL_CHUNKS",
            "not-an-int",
        ),
    ],
)
def test_runtime_rejects_invalid_evidence_workload_budget(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    async def run() -> None:
        configure_real_planner(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
            "openai-compatible",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL",
            "https://example.test/v1",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_API_KEY",
            "evidence-secret",
        )
        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MODEL",
            "test-evidence",
        )

        monkeypatch.setenv(
            name,
            value,
        )

        service = EnterpriseResearchService()

        with pytest.raises(
            RuntimeError,
            match=("Enterprise Research evidence numeric settings are invalid"),
        ):
            await service.start(create_runtime_deps())

    asyncio.run(run())


def _configure_evidence_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_real_planner(monkeypatch)

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_BACKEND",
        "openai-compatible",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL",
        "https://example.test/v1",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_API_KEY",
        "evidence-secret",
    )

    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_EVIDENCE_MODEL",
        "test-evidence",
    )


def test_runtime_uses_default_evidence_max_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _configure_evidence_backend(monkeypatch)

        monkeypatch.delenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY",
            raising=False,
        )

        captured: dict[str, int] = {}

        original_service = plugin_module.ResearchEvidenceExtractionService

        def build_service(
            *args,
            max_concurrency: int,
            **kwargs,
        ):
            captured["max_concurrency"] = max_concurrency

            return original_service(
                *args,
                max_concurrency=(max_concurrency),
                **kwargs,
            )

        monkeypatch.setattr(
            plugin_module,
            "ResearchEvidenceExtractionService",
            build_service,
        )

        service = EnterpriseResearchService()

        await service.start(create_runtime_deps())

        try:
            assert captured == {"max_concurrency": 4}

        finally:
            await service.stop()

    asyncio.run(run())


def test_runtime_applies_custom_evidence_max_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _configure_evidence_backend(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY",
            "3",
        )

        captured: dict[str, int] = {}

        original_service = plugin_module.ResearchEvidenceExtractionService

        def build_service(
            *args,
            max_concurrency: int,
            **kwargs,
        ):
            captured["max_concurrency"] = max_concurrency

            return original_service(
                *args,
                max_concurrency=(max_concurrency),
                **kwargs,
            )

        monkeypatch.setattr(
            plugin_module,
            "ResearchEvidenceExtractionService",
            build_service,
        )

        service = EnterpriseResearchService()

        await service.start(create_runtime_deps())

        try:
            assert captured == {"max_concurrency": 3}

        finally:
            await service.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "not-an-int",
    ],
)
def test_runtime_rejects_invalid_evidence_max_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    async def run() -> None:
        _configure_evidence_backend(monkeypatch)

        monkeypatch.setenv(
            "ENTERPRISE_RESEARCH_EVIDENCE_MAX_CONCURRENCY",
            value,
        )

        service = EnterpriseResearchService()

        with pytest.raises(
            RuntimeError,
            match=("Enterprise Research evidence numeric settings are invalid"),
        ):
            await service.start(create_runtime_deps())

    asyncio.run(run())
