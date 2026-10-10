"""3G disabled-by-default verification cannot silently bypass 3F wiring."""

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


def test_unknown_claim_verification_provider_is_rejected(monkeypatch):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND", "invalid")
        with pytest.raises(RuntimeError, match="ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND"):
            await EnterpriseResearchService().start(runtime_deps())

    asyncio.run(exercise())


def test_verification_requires_explicit_claim_generation(monkeypatch):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND", "openai-compatible")
        with pytest.raises(RuntimeError, match="requires enabled 3F"):
            await EnterpriseResearchService().start(runtime_deps())

    asyncio.run(exercise())
