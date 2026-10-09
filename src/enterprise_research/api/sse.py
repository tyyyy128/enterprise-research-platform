"""Wire-format helpers for Enterprise Research Server-Sent Events."""

from __future__ import annotations

import json

from enterprise_research.application.run_events import ResearchRunEvent


def encode_run_event(event: ResearchRunEvent | None) -> str:
    """Encode one SSE frame; heartbeats deliberately have no event ID."""
    if event is None:
        return ": keep-alive\n\n"

    lines = []
    if event.sequence is not None:
        lines.append(f"id: {event.sequence}")
    lines.append(f"event: {event.event}")
    lines.append(f"data: {json.dumps(event.data, ensure_ascii=False, default=str, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"
