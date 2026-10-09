"""In-process executor for research runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID, uuid4

from enterprise_research.application.run_events import ResearchRunEventBus
from enterprise_research.application.run_lease import (
    ResearchRunLease,
    ResearchRunLeaseLostError,
    validate_lease_ttl,
)
from enterprise_research.application.run_service import (
    ResearchRunApplicationService,
)
from enterprise_research.application.stage_journal import ResearchStageOutcome
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
        event_bus: ResearchRunEventBus | None = None,
        *,
        lease_ttl_seconds: int = 30,
    ) -> None:
        validate_lease_ttl(lease_ttl_seconds)
        self._lease_ttl_seconds = lease_ttl_seconds
        self._owner_id = uuid4()
        self._application = application
        self._stage_runner = stage_runner
        self._event_bus = event_bus
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

        if self._event_bus is not None:
            self._event_bus.register_run(task_id, run_id)

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

    async def _heartbeat(
        self,
        lease: ResearchRunLease,
        executing_task: asyncio.Task[None],
        stop: asyncio.Event,
    ) -> None:
        """Renew in parallel with long-running stage work.

        A lost lease cancels the execution coroutine; it must never mark the
        run FAILED with the stale token. External effects still require
        idempotency or fencing at their own persistence boundary.
        """
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._lease_ttl_seconds / 3)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._application.renew_run_lease(lease, ttl_seconds=self._lease_ttl_seconds)
            except Exception:
                logger.exception("Research run lease heartbeat failed for %s", lease.run_id)
                executing_task.cancel()
                return
            if renewed is None:
                if stop.is_set():
                    return
                logger.warning("Research run lease lost for %s", lease.run_id)
                executing_task.cancel()
                return

    async def _execute(
        self,
        task_id: UUID,
        run_id: UUID,
    ) -> None:
        lease: ResearchRunLease | None = None
        heartbeat: asyncio.Task[None] | None = None
        stop = asyncio.Event()
        try:
            # The row must be atomically claimed BEFORE entering any stage.
            lease = await self._application.claim_run_lease(
                task_id,
                run_id,
                self._owner_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            if lease is None:
                if self._event_bus is not None:
                    await self._event_bus.publish_interruption(task_id, run_id)
                return

            current_task = asyncio.current_task()
            assert current_task is not None
            heartbeat = asyncio.create_task(
                self._heartbeat(lease, current_task, stop),
                name=f"enterprise-research-heartbeat:{run_id}",
            )

            for stage in self._STAGES:
                await self._application.advance_run(task_id, run_id, stage, lease=lease)
                if stage == ResearchRunStatus.COMPLETED:
                    # Avoid renewal racing the terminal commit/SSE event.
                    stop.set()

                if stage != ResearchRunStatus.COMPLETED:
                    # A persisted stage status only proves stage ENTRY. The
                    # journal records the actual attempt before work can begin.
                    await self._application.start_stage(task_id, run_id, stage, lease=lease)

                if self._event_bus is not None:
                    await self._event_bus.publish_stage(task_id, run_id, stage)

                if stage != ResearchRunStatus.COMPLETED:
                    try:
                        await self._stage_runner.run_stage(task_id, run_id, stage)
                    except ResearchRunLeaseLostError:
                        raise
                    except Exception as exc:
                        message = str(exc).strip() or exc.__class__.__name__
                        await self._application.finish_stage(
                            task_id, run_id, stage, lease=lease, outcome=ResearchStageOutcome.FAILED, error=message
                        )
                        raise
                    await self._application.finish_stage(
                        task_id, run_id, stage, lease=lease, outcome=ResearchStageOutcome.COMPLETED
                    )

        except asyncio.CancelledError:
            if self._event_bus is not None:
                await self._event_bus.publish_interruption(task_id, run_id)
            raise

        except ResearchRunLeaseLostError:
            # The run might now belong to another worker. Never persist a
            # FAILED transition (or a success event) using a stale token.
            logger.warning("Ignoring stale run transition for %s", run_id)
            if self._event_bus is not None:
                await self._event_bus.publish_interruption(task_id, run_id)

        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            try:
                if lease is None:
                    raise ResearchRunLeaseLostError("Run execution never acquired a lease.")
                await self._application.fail_run(task_id, run_id, message, lease=lease)
            except ResearchRunLeaseLostError:
                logger.warning("Cannot fail run %s after losing its lease", run_id)
                if self._event_bus is not None:
                    await self._event_bus.publish_interruption(task_id, run_id)
            except Exception:
                logger.exception("Failed to persist research run failure for %s", run_id)
                if self._event_bus is not None:
                    await self._event_bus.publish_interruption(task_id, run_id)
                raise
            else:
                if self._event_bus is not None:
                    await self._event_bus.publish_failure(task_id, run_id, message)

        finally:
            stop.set()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if lease is not None:
                try:
                    await self._application.release_run_lease(lease)
                except Exception:
                    logger.exception("Could not release lease for research run %s", run_id)

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
