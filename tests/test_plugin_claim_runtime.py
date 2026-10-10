"""3F startup requires explicit provider wiring, never a silent no-op."""

from __future__ import annotations

import asyncio

import pytest
from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps, HostPolicySnapshot

from enterprise_research.plugin import EnterpriseResearchService


def runtime_deps():
    return ExtensionRuntimeDeps(
        app_store=ExtensionData("app"),
        policy=HostPolicySnapshot(max_subagents_per_run=3),
        session_factory=object(),
    )


def test_claim_backend_invalid_name_fails_on_start(monkeypatch):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "unknown-provider")
        service = EnterpriseResearchService()
        with pytest.raises(RuntimeError, match="ENTERPRISE_RESEARCH_CLAIM_BACKEND"):
            await service.start(runtime_deps())

    asyncio.run(exercise())


def test_claim_backend_rejects_disabled_planner(monkeypatch):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "openai-compatible")
        service = EnterpriseResearchService()
        with pytest.raises(RuntimeError, match="real planner"):
            await service.start(runtime_deps())

    asyncio.run(exercise())


def test_claim_backend_rejects_memory_repository(monkeypatch):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "openai-compatible")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BASE_URL", "https://example.test/v1")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_API_KEY", "test-key")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_MODEL", "mock-plan")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_SEARCH_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_EVIDENCE_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "openai-compatible")
        service = EnterpriseResearchService()
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            await service.start(runtime_deps())

    asyncio.run(exercise())
