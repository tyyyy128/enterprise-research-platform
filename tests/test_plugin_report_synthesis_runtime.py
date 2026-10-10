"""3H synthesis backend is explicit and cannot bypass 3G verification wiring."""

from __future__ import annotations

import asyncio

import pytest
from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps, HostPolicySnapshot

from enterprise_research.plugin import EnterpriseResearchService


def _deps():
    return ExtensionRuntimeDeps(
        app_store=ExtensionData("research"),
        policy=HostPolicySnapshot(max_subagents_per_run=3),
        session_factory=object(),
    )


@pytest.mark.parametrize(
    "writer, expected",
    [
        ("openai-compatible", "requires PostgreSQL, a real planner and enabled 3G"),
        ("made-up", "ENTERPRISE_RESEARCH_WRITER_BACKEND"),
    ],
)
def test_report_synthesis_rejects_memory_or_unknown_backends(monkeypatch, writer, expected):
    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "openai-compatible")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BASE_URL", "https://example.org/v1")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_API_KEY", "test")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_MODEL", "mock-plan")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_SEARCH_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_RETRIEVAL_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_EVIDENCE_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_WRITER_BACKEND", writer)
        with pytest.raises(RuntimeError, match=expected):
            await EnterpriseResearchService().start(_deps())

    asyncio.run(exercise())
