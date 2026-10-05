"""In-process executor for research runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from enterprise_research.application.run_service import (
    ResearchRunApplicationService,
)
from enterprise_research.domain import ResearchRunStatus

logger = logging.getLogger(__name__)


class ResearchStageRunner(Protocol):
    """Execute the work associated with one research stage."""

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        """Execute one stage."""


class SkeletonResearchStageRunner:
    """Placeholder stage runner used until real agents are wired in."""

    async def run_stage(
        self,
        task_id: UUID,
        run_id: UUID,
        stage: ResearchRunStatus,
    ) -> None:
        """Yield once without performing external research work."""
        await asyncio.sleep(0)


class ResearchRunExecutor:
    """Run research stages asynchronously inside the current process."""

    _STAGES = (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
        ResearchRunStatus.COMPLETED,
    )

    def __init__(
        self,
        application: ResearchRunApplicationService,
        stage_runner: ResearchStageRunner,
    ) -> None:
        self._application = application
        self._stage_runner = stage_runner
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    @property
    def active_count(self) -> int:
        """Return the number of currently scheduled runs."""
        return len(self._tasks)

    def start(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        """Schedule one research run without waiting for completion."""
        existing = self._tasks.get(run_id)

        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._execute(task_id, run_id),
            name=f"enterprise-research-run:{run_id}",
        )

        self._tasks[run_id] = task
        task.add_done_callback(
            lambda done_task: self._task_done(
                run_id,
                done_task,
            )
        )

    async def shutdown(self) -> None:
        """Cancel and drain all in-process research tasks."""
        tasks = list(self._tasks.values())

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        self._tasks.clear()

    async def _execute(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        try:
            for stage in self._STAGES:
                await self._application.advance_run(
                    task_id,
                    run_id,
                    stage,
                )

                if stage != ResearchRunStatus.COMPLETED:
                    await self._stage_runner.run_stage(
                        task_id,
                        run_id,
                        stage,
                    )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__

            try:
                await self._application.fail_run(
                    task_id,
                    run_id,
                    message,
                )
            except Exception:
                logger.exception(
                    "Failed to persist research run failure for %s",
                    run_id,
                )
                raise

    def _task_done(
        self,
        run_id: UUID,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

        if task.cancelled():
            return

        exception = task.exception()

        if exception is not None:
            logger.error(
                "Research run task %s crashed",
                run_id,
                exc_info=(
                    type(exception),
                    exception,
                    exception.__traceback__,
                ),
            )
