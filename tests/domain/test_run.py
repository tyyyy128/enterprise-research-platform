from __future__ import annotations

from uuid import uuid4

import pytest

from enterprise_research.domain import (
    InvalidResearchRunTransitionError,
    ResearchRun,
    ResearchRunStatus,
)


def test_run_starts_pending() -> None:
    run = ResearchRun.create(uuid4())

    assert run.status == ResearchRunStatus.PENDING
    assert run.started_at is None
    assert run.completed_at is None
    assert run.error is None
    assert run.is_terminal is False


def test_run_advances_through_complete_pipeline() -> None:
    run = ResearchRun.create(uuid4())

    for next_status in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
        ResearchRunStatus.COMPLETED,
    ):
        run.advance(next_status)

    assert run.status == ResearchRunStatus.COMPLETED
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.error is None
    assert run.is_terminal is True


def test_run_rejects_skipping_pipeline_stage() -> None:
    run = ResearchRun.create(uuid4())

    with pytest.raises(
        InvalidResearchRunTransitionError,
        match="Cannot transition research run",
    ):
        run.advance(ResearchRunStatus.SEARCHING)


def test_run_can_fail_from_active_stage() -> None:
    run = ResearchRun.create(uuid4())

    run.advance(ResearchRunStatus.PLANNING)
    run.fail("provider unavailable")

    assert run.status == ResearchRunStatus.FAILED
    assert run.error == "provider unavailable"
    assert run.completed_at is not None
    assert run.is_terminal is True


def test_run_rejects_empty_failure_message() -> None:
    run = ResearchRun.create(uuid4())

    with pytest.raises(
        ValueError,
        match="requires an error message",
    ):
        run.fail("   ")


def test_terminal_run_cannot_fail_or_advance() -> None:
    run = ResearchRun.create(uuid4())

    for next_status in (
        ResearchRunStatus.PLANNING,
        ResearchRunStatus.SEARCHING,
        ResearchRunStatus.EXTRACTING,
        ResearchRunStatus.VERIFYING,
        ResearchRunStatus.WRITING,
        ResearchRunStatus.COMPLETED,
    ):
        run.advance(next_status)

    with pytest.raises(
        InvalidResearchRunTransitionError,
    ):
        run.fail("too late")

    with pytest.raises(
        InvalidResearchRunTransitionError,
    ):
        run.advance(ResearchRunStatus.PLANNING)
