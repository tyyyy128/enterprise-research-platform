from __future__ import annotations

import asyncio

import pytest
from deerflow_extension_api import (
    ExtensionData,
    ExtensionRuntimeDeps,
    HostPolicySnapshot,
)

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
