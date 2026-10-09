"""Read-only LangGraph checkpoint boundary for run-scoped research stages.

LangGraph checkpoints are *graph state*, not proof that business data committed.
Only the lease-fenced PostgreSQL Run and Stage Journal authorize execution.
The reader is optional until a host-approved graph saver is explicitly wired.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.stage_journal import validate_stage
from enterprise_research.domain import ResearchRunStatus


class ResearchGraphCheckpointConflictError(RuntimeError):
    """A returned checkpoint does not belong to the requested run/generation."""


@dataclass(frozen=True, slots=True)
class ResearchGraphCheckpointBinding:
    """Run, Task, Stage and fencing generation mapped to one Graph namespace."""

    task_id: UUID
    run_id: UUID
    stage: ResearchRunStatus
    fencing_token: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, UUID) or not isinstance(self.run_id, UUID):
            raise TypeError("Graph checkpoint Task and Run identifiers must be UUIDs.")
        validate_stage(self.stage)
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise ValueError("Graph checkpoint fencing generation must be a positive integer.")

    @classmethod
    def for_lease(
        cls, task_id: UUID, stage: ResearchRunStatus, lease: ResearchRunLease
    ) -> ResearchGraphCheckpointBinding:
        """Bind Graph execution to an already-acquired Run lease; never claim it."""
        return cls(task_id, lease.run_id, stage, lease.fencing_token)

    @property
    def thread_id(self) -> str:
        """Isolate research runs from DeerFlow conversation thread IDs."""
        return f"enterprise-research:v1:{self.task_id}:{self.run_id}"

    @property
    def checkpoint_ns(self) -> str:
        """Keep older fencing generations from overwriting a replacement's state."""
        return f"research/{self.stage.value}/generation/{self.fencing_token}"

    def runnable_config(self) -> dict[str, dict[str, str]]:
        """Return LangGraph-compatible config without importing its runtime."""
        return {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": self.checkpoint_ns}}


@dataclass(frozen=True, slots=True)
class ResearchGraphCheckpointReference:
    """A checkpoint identifier, deliberately excluding the Graph state payload."""

    binding: ResearchGraphCheckpointBinding
    checkpoint_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ResearchGraphCheckpointBinding):
            raise TypeError("Checkpoint reference requires a validated binding.")
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id.strip():
            raise ValueError("Checkpoint reference requires a nonblank checkpoint ID.")


class ResearchGraphCheckpointReader(Protocol):
    """Optional observation dependency for Safe Resume; never a writer."""

    async def latest(self, binding: ResearchGraphCheckpointBinding) -> ResearchGraphCheckpointReference | None:
        """Return a validated checkpoint reference, or None if absent."""


class AsyncLangGraphCheckpointSaver(Protocol):
    """The public BaseCheckpointSaver async read API (structural protocol)."""

    async def aget_tuple(self, config: dict[str, dict[str, str]]) -> object | None:
        """Read the latest checkpoint tuple for this namespace."""


class LangGraphCheckpointReader:
    """Adapt a supplied async LangGraph saver without importing LangGraph.

    This class does not install a saver, execute a Graph, or give checkpoint
    contents authority over Run status. No global/host saver is presumed.
    """

    def __init__(self, saver: AsyncLangGraphCheckpointSaver) -> None:
        if not callable(getattr(saver, "aget_tuple", None)):
            raise TypeError("Checkpoint saver must implement async aget_tuple(config).")
        self._saver = saver

    async def latest(self, binding: ResearchGraphCheckpointBinding) -> ResearchGraphCheckpointReference | None:
        record = await self._saver.aget_tuple(binding.runnable_config())
        if record is None:
            return None

        config = getattr(record, "config", None)
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        if not isinstance(configurable, Mapping):
            raise ResearchGraphCheckpointConflictError("Checkpoint lacks its persisted Graph configuration.")
        if (
            configurable.get("thread_id") != binding.thread_id
            or configurable.get("checkpoint_ns") != binding.checkpoint_ns
        ):
            raise ResearchGraphCheckpointConflictError("Checkpoint belongs to a different Run or generation.")

        checkpoint = getattr(record, "checkpoint", None)
        if not isinstance(checkpoint, Mapping):
            raise ResearchGraphCheckpointConflictError("Checkpoint has no Graph payload identity.")
        config_id = configurable.get("checkpoint_id")
        payload_id = checkpoint.get("id")
        if any(
            item is not None and (not isinstance(item, str) or not item.strip()) for item in (config_id, payload_id)
        ):
            raise ResearchGraphCheckpointConflictError("Checkpoint identifier is malformed.")
        if config_id is not None and payload_id is not None and config_id != payload_id:
            raise ResearchGraphCheckpointConflictError("Checkpoint identifier conflicts with Graph payload.")
        checkpoint_id = config_id if config_id is not None else payload_id
        if checkpoint_id is None:
            raise ResearchGraphCheckpointConflictError("Checkpoint has no identity.")
        return ResearchGraphCheckpointReference(binding, checkpoint_id)
