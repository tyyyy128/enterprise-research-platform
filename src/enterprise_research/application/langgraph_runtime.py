"""Opt-in, extension-owned LangGraph stage execution.

The DeerFlow ExtensionRuntimeDeps contract does not expose its host graph saver.
This adapter compiles a *separate* LangGraph for each leased research stage,
using a caller-owned async saver. Business completion remains exclusively in
the fenced Stage Journal; graph checkpoints are observational, not receipts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypedDict
from uuid import UUID

from enterprise_research.application.executor import ResearchStageRunner
from enterprise_research.application.graph_checkpoint import (
    AsyncLangGraphCheckpointSaver,
    LangGraphCheckpointReader,
    ResearchGraphCheckpointBinding,
    ResearchGraphCheckpointConflictError,
)
from enterprise_research.application.run_lease import ResearchRunLease, ResearchRunLeaseLostError
from enterprise_research.domain import ResearchRunStatus


class ResearchGraphStageState(TypedDict):
    """No model output or research payload is stored in Graph state."""

    task_id: str
    run_id: str
    stage: str
    fencing_token: int
    stage_returned: bool


class AsyncStageGraph(Protocol):
    async def ainvoke(self, input: ResearchGraphStageState, config: dict[str, dict[str, str]]) -> Any:
        """Execute one bounded stage with a LangGraph runnable config."""


GraphFactory = Callable[
    [Callable[[ResearchGraphStageState], Awaitable[dict[str, bool]]], AsyncLangGraphCheckpointSaver], AsyncStageGraph
]


def _compile_stage_graph(
    execute: Callable[[ResearchGraphStageState], Awaitable[dict[str, bool]]], saver: AsyncLangGraphCheckpointSaver
) -> AsyncStageGraph:
    """Build the real LangGraph only when opted in (not on plugin import)."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ResearchGraphStageState)
    builder.add_node("execute_stage", execute)
    builder.add_edge(START, "execute_stage")
    builder.add_edge("execute_stage", END)
    return builder.compile(checkpointer=saver)


class ResearchLangGraphStageRunner:
    """Execute a fresh fenced stage through a real checkpointed Graph.

    Never invoke an existing checkpoint thread: LangGraph may otherwise
    return the previous completed result without running the business stage.
    After an ambiguous crash the journal *pauses* recovery; a new generation
    may only run a later stage following Safe Resume's artifact proof.
    """

    def __init__(
        self,
        delegate: ResearchStageRunner,
        saver: AsyncLangGraphCheckpointSaver,
        *,
        graph_factory: GraphFactory = _compile_stage_graph,
    ) -> None:
        self._delegate = delegate
        self._reader = LangGraphCheckpointReader(saver)
        self._saver = saver
        self._graph_factory = graph_factory

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
        *,
        lease: ResearchRunLease | None = None,
    ) -> None:
        if lease is None or lease.run_id != run_id:
            raise ResearchRunLeaseLostError("A Graph stage requires its own Run's fenced execution lease.")
        binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)

        # No prior Graph state should exist for this fresh fencing generation.
        # Do not turn a previously checkpointed stage into an implicit replay.
        if await self._reader.latest(binding) is not None:
            raise ResearchGraphCheckpointConflictError("Graph stage thread already contains a checkpoint.")

        state: ResearchGraphStageState = {
            "task_id": str(task_id),
            "run_id": str(run_id),
            "stage": stage.value,
            "fencing_token": lease.fencing_token,
            "stage_returned": False,
        }

        async def execute_stage(observed: ResearchGraphStageState) -> dict[str, bool]:
            # Fail closed if a Graph checkpoint injects a different identity.
            if any(observed.get(name) != value for name, value in state.items() if name != "stage_returned"):
                raise ResearchGraphCheckpointConflictError("Graph stage input identity is inconsistent with the lease.")
            await self._delegate.run_stage(task_id, run_id, stage, lease=lease)
            return {"stage_returned": True}

        graph = self._graph_factory(execute_stage, self._saver)
        result = await graph.ainvoke(state, config=binding.runnable_config())
        if not isinstance(result, dict) or result.get("stage_returned") is not True:
            raise ResearchGraphCheckpointConflictError("Graph did not confirm stage node completion.")

        # The subsequent Stage Journal write is *not* atomic with this read.
        # A failure here leaves the journal in doubt, which Safe Resume pauses.
        if await self._reader.latest(binding) is None:
            raise ResearchGraphCheckpointConflictError("Graph returned without a durable checkpoint.")
