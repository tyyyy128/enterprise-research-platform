"""4C: fake saver/graph injection tests for fenced LangGraph stage wiring.

No host Checkpointer or network is assumed by these deterministic tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from enterprise_research.application import (
    LangGraphCheckpointReader,
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointConflictError,
    ResearchLangGraphStageRunner,
    ResearchStageUnavailableError,
)
from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRunStatus


class Saver:
    def __init__(self):
        self.records = {}
        self.failure = None
        self.reads = []

    async def aget_tuple(self, config):
        if self.failure is not None:
            raise self.failure
        self.reads.append(config)
        return self.records.get((config["configurable"]["thread_id"], config["configurable"]["checkpoint_ns"]))

    def write(self, config):
        key = (config["configurable"]["thread_id"], config["configurable"]["checkpoint_ns"])
        checkpoint_id = f"checkpoint-{len(self.records) + 1}"
        self.records[key] = SimpleNamespace(
            config={"configurable": {**config["configurable"], "checkpoint_id": checkpoint_id}},
            checkpoint={"id": checkpoint_id, "channel_values": {"should_not_be_logged": True}},
        )


class Delegate:
    def __init__(self):
        self.calls = []

    async def run_stage(self, task_id, run_id, stage, *, lease=None):
        self.calls.append((task_id, run_id, stage, lease))


class FakeGraph:
    def __init__(self, node, saver, *, when="after", mutate=None):
        self.node = node
        self.saver = saver
        self.when = when
        self.mutate = mutate

    async def ainvoke(self, state, config):
        self.saver.last_input = state.copy()
        if self.when == "before":
            self.saver.write(config)
        if self.mutate is not None:
            state = {**state, **self.mutate}
        output = await self.node(state)
        if self.when == "after":
            self.saver.write(config)
        return {**state, **output}


def args(token=1):
    task_id, run_id = uuid4(), uuid4()
    lease = ResearchRunLease(run_id, uuid4(), token, datetime.now(UTC) + timedelta(minutes=1))
    return task_id, run_id, ResearchRunStatus.PLANNING, lease


def graph_factory(*, when="after", mutate=None):
    return lambda node, saver: FakeGraph(node, saver, when=when, mutate=mutate)


def test_stage_graph_writes_checkpoint_and_rejects_same_generation_replay():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=graph_factory())
        await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert delegate.calls == [(task_id, run_id, stage, lease)]
        binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)
        observed = await LangGraphCheckpointReader(saver).latest(binding)
        assert observed is not None and observed.checkpoint_id == "checkpoint-1"
        assert len(saver.records) == 1
        with pytest.raises(ResearchGraphCheckpointConflictError, match="already contains"):
            await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert len(delegate.calls) == 1
        # A new token means a different namespace; no hidden cross-generation continuation.
        next_lease = ResearchRunLease(run_id, uuid4(), 2, lease.expires_at)
        await runner.run_stage(task_id, run_id, stage, lease=next_lease)
        assert len(delegate.calls) == 2
        assert len(saver.records) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("wrong", [None, "another_run"])
def test_graph_refuses_missing_or_wrong_lease(wrong):
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        if wrong is not None:
            lease = ResearchRunLease(uuid4(), lease.owner_id, lease.fencing_token, lease.expires_at)
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=graph_factory())
        with pytest.raises(ResearchRunLeaseLostError):
            await runner.run_stage(task_id, run_id, stage, lease=None if wrong is None else lease)
        assert not delegate.calls and not saver.reads

    asyncio.run(exercise())


def test_graph_rejects_checkpoint_reader_errors_before_side_effects():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        saver.failure = ConnectionError("graph db offline")
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=graph_factory())
        with pytest.raises(ConnectionError, match="graph db offline"):
            await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert not delegate.calls

    asyncio.run(exercise())


def test_graph_rejects_checkpoint_input_identity_change_before_stage():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        factory = graph_factory(mutate={"run_id": str(uuid4())})
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=factory)
        with pytest.raises(ResearchGraphCheckpointConflictError, match="identity"):
            await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert not delegate.calls

    asyncio.run(exercise())


def test_checkpoint_before_stage_is_not_a_business_commit():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=graph_factory(when="before"))
        await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert len(delegate.calls) == 1
        binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)
        assert await LangGraphCheckpointReader(saver).latest(binding) is not None

    asyncio.run(exercise())


def test_graph_refuses_missing_final_persisted_checkpoint():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=graph_factory(when="never"))
        with pytest.raises(ResearchGraphCheckpointConflictError, match="durable checkpoint"):
            await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert len(delegate.calls) == 1  # Ambiguous result: Safe Resume MUST pause unless Journal/artifact prove it.

    asyncio.run(exercise())


def test_graph_does_not_persist_raw_research_content():
    async def exercise():
        task_id, run_id, stage, lease = args()
        saver = Saver()
        runner = ResearchLangGraphStageRunner(Delegate(), saver, graph_factory=graph_factory())
        await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert set(saver.last_input) == {"task_id", "run_id", "stage", "fencing_token", "stage_returned"}
        assert saver.last_input["run_id"] == str(run_id)
        assert all("research_content" not in str(item) for item in saver.records.values())

    asyncio.run(exercise())


def test_root_graph_namespace_normalization_keeps_checkpoint_visible():
    """Top-level LangGraph resets checkpoint_ns to the empty root namespace."""

    class RootGraph(FakeGraph):
        async def ainvoke(self, state, config):
            actual = {
                **config,
                "configurable": {**config["configurable"], "checkpoint_ns": ""},
            }
            return await super().ainvoke(state, actual)

    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = Saver(), Delegate()
        runner = ResearchLangGraphStageRunner(delegate, saver, graph_factory=RootGraph)
        await runner.run_stage(task_id, run_id, stage, lease=lease)
        binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)
        assert binding.checkpoint_ns == ""
        assert (binding.thread_id, "") in saver.records
        assert await LangGraphCheckpointReader(saver).latest(binding) is not None
        assert len(delegate.calls) == 1

    asyncio.run(exercise())


def test_real_langgraph_in_memory_checkpoint_if_available():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    async def exercise():
        task_id, run_id, stage, lease = args()
        saver, delegate = InMemorySaver(), Delegate()
        runner = ResearchLangGraphStageRunner(delegate, saver)
        await runner.run_stage(task_id, run_id, stage, lease=lease)
        assert len(delegate.calls) == 1
        binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)
        reader = LangGraphCheckpointReader(saver)
        assert await reader.latest(binding) is not None
        assert binding.checkpoint_ns == ""
        assert await saver.aget_tuple(binding.runnable_config()) is not None
        assert (
            await reader.latest(
                ResearchGraphCheckpointBinding(task_id, run_id, ResearchRunStatus.SEARCHING, lease.fencing_token)
            )
            is None
        )
        assert (
            await reader.latest(ResearchGraphCheckpointBinding(task_id, run_id, stage, lease.fencing_token + 1)) is None
        )
        # Even a new runner after a simulated process restart cannot implicitly replay.
        another = ResearchLangGraphStageRunner(delegate, saver)
        with pytest.raises(ResearchGraphCheckpointConflictError):
            await another.run_stage(task_id, run_id, stage, lease=lease)
        assert len(delegate.calls) == 1

    asyncio.run(exercise())


def test_plugin_graph_backend_is_opt_in_and_invalid_options_fail_closed(monkeypatch):
    from deerflow_extension_api import ExtensionData, ExtensionRuntimeDeps

    from enterprise_research.plugin import EnterpriseResearchService

    async def exercise():
        monkeypatch.setenv("ENTERPRISE_RESEARCH_REPOSITORY", "memory")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_PLANNER_BACKEND", "disabled")
        monkeypatch.setenv("ENTERPRISE_RESEARCH_SEARCH_BACKEND", "disabled")
        deps = ExtensionRuntimeDeps(app_store=ExtensionData("test"))

        monkeypatch.setenv("ENTERPRISE_RESEARCH_GRAPH_BACKEND", "disabled")
        service = EnterpriseResearchService()
        await service.start(deps)
        assert (await service.require_run_executor()) is not None
        await service.stop()

        for invalid in ["postgres", "unknown"]:
            monkeypatch.setenv("ENTERPRISE_RESEARCH_GRAPH_BACKEND", invalid)
            service = EnterpriseResearchService()
            with pytest.raises(RuntimeError):
                await service.start(deps)
            assert service._deps is None
            assert service._run_executor is None
            await service.stop()

    asyncio.run(exercise())


def test_graph_stage_never_turns_unavailable_delegate_into_success_checkpoint():
    class Unavailable:
        async def run_stage(self, task_id, run_id, stage, *, lease=None):
            raise ResearchStageUnavailableError("WRITING has no report writer")

    async def exercise():
        task_id, run_id, _stage, lease = args()
        saver = Saver()
        runner = ResearchLangGraphStageRunner(Unavailable(), saver, graph_factory=graph_factory())
        with pytest.raises(ResearchStageUnavailableError, match="WRITING"):
            await runner.run_stage(task_id, run_id, ResearchRunStatus.WRITING, lease=lease)
        assert not saver.records

    asyncio.run(exercise())
