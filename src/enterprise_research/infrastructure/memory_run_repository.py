"""In-memory repository for research execution runs and fenced leases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from enterprise_research.application.run_lease import (
    ResearchRunLease,
    ResearchRunLeaseLostError,
    validate_lease_ttl,
)
from enterprise_research.domain import ResearchRun, ResearchRunStatus
from enterprise_research.domain.models import utc_now


class InMemoryResearchRunRepository:
    """Process-local adapter with the same ownership contract as PostgreSQL."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._items: dict[UUID, ResearchRun] = {}
        self._leases: dict[UUID, ResearchRunLease] = {}
        self._fences: dict[UUID, int] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def save(self, run: ResearchRun) -> None:
        """Persist a detached run only if it has never been fenced."""
        async with self._lock:
            if self._fences.get(run.run_id, 0):
                raise ResearchRunLeaseLostError("Unfenced update refused for a claimed research run.")
            # Preserve the legacy in-memory fixture behavior before first claim;
            # once fenced, task identity is checked by save_guarded.
            self._items[run.run_id] = run.model_copy(deep=True)

    async def get(self, run_id: UUID) -> ResearchRun | None:
        """Return a detached snapshot."""
        async with self._lock:
            run = self._items.get(run_id)
            return run.model_copy(deep=True) if run is not None else None

    async def list_unfinished(self, *, limit: int, task_id: UUID | None = None) -> list[ResearchRun]:
        """Return detached nonterminal snapshots ordered by update time and ID."""
        if limit < 1:
            raise ValueError("Recovery scan limit must be positive.")
        async with self._lock:
            unfinished = sorted(
                (
                    run
                    for run in self._items.values()
                    if not run.is_terminal and (task_id is None or run.task_id == task_id)
                ),
                key=lambda run: (run.updated_at, run.run_id),
            )
            return [run.model_copy(deep=True) for run in unfinished[:limit]]

    async def claim_lease(
        self, run_id: UUID, owner_id: UUID, *, ttl_seconds: int, allow_inflight: bool = False
    ) -> ResearchRunLease | None:
        """Claim a new generation, unless another unexpired owner holds it."""
        validate_lease_ttl(ttl_seconds)
        async with self._lock:
            run = self._items.get(run_id)
            if run is None or run.is_terminal or (not allow_inflight and run.status != ResearchRunStatus.PENDING):
                return None
            now = self._clock()
            old = self._leases.get(run_id)
            if old is not None and old.expires_at > now:
                return None
            fence = self._fences.get(run_id, 0) + 1
            current = ResearchRunLease(run_id, owner_id, fence, now + timedelta(seconds=ttl_seconds))
            self._fences[run_id] = fence
            self._leases[run_id] = current
            return current

    async def renew_lease(self, lease: ResearchRunLease, *, ttl_seconds: int) -> ResearchRunLease | None:
        """Renew only the live owner and generation."""
        validate_lease_ttl(ttl_seconds)
        async with self._lock:
            run = self._items.get(lease.run_id)
            current = self._leases.get(lease.run_id)
            now = self._clock()
            if run is None or run.is_terminal or not self._matches(current, lease, now):
                return None
            updated = ResearchRunLease(
                lease.run_id, lease.owner_id, lease.fencing_token, now + timedelta(seconds=ttl_seconds)
            )
            self._leases[lease.run_id] = updated
            return updated

    async def release_lease(self, lease: ResearchRunLease) -> bool:
        """Never clear another generation's lease."""
        async with self._lock:
            current = self._leases.get(lease.run_id)
            if not self._matches(current, lease, self._clock()):
                return False
            self._leases.pop(lease.run_id, None)
            return True

    async def save_guarded(
        self, run: ResearchRun, lease: ResearchRunLease, *, expected_status: ResearchRunStatus
    ) -> None:
        """Check token, expiry and predecessor status inside the same lock."""
        async with self._lock:
            current = self._items.get(run.run_id)
            held = self._leases.get(run.run_id)
            if (
                current is None
                or current.task_id != run.task_id
                or current.is_terminal
                or current.status != expected_status
                or not self._matches(held, lease, self._clock())
            ):
                raise ResearchRunLeaseLostError("Run transition refused: lease lost or stale status.")
            self._items[run.run_id] = run.model_copy(deep=True)

    @staticmethod
    def _matches(current: ResearchRunLease | None, supplied: ResearchRunLease, now: datetime) -> bool:
        return (
            current is not None
            and current.run_id == supplied.run_id
            and current.owner_id == supplied.owner_id
            and current.fencing_token == supplied.fencing_token
            and current.expires_at > now
        )
