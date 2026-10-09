"""Versioned execution ownership for a single research run.

A lease is a capability for guarded *run lifecycle* writes, not proof that
external stage effects are safe to repeat. The repository is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ResearchRunLeaseLostError(RuntimeError):
    """The caller does not own an unexpired, current-generation run lease."""


@dataclass(frozen=True, slots=True)
class ResearchRunLease:
    """Immutable owner and generation returned by a successful atomic claim."""

    run_id: UUID
    owner_id: UUID
    fencing_token: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise ValueError("Fencing token must be positive.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Lease expiration must be timezone-aware.")


def validate_lease_ttl(ttl_seconds: int) -> None:
    """Keep integer-only intervals short enough for heartbeat scheduling."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
        raise ValueError("Run lease ttl_seconds must be an integer between 1 and 3600.")
