"""5A: real SIGKILL of an isolated worker at durable execution boundaries.

Must be explicitly opted in, with two distinct *local test databases*. Only
random test Task/Run records and one deterministic Graph Thread are deleted.
Never kills PostgreSQL, DeerFlow, or any process other than its own child.
"""

from __future__ import annotations

import asyncio
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from sqlalchemy import delete

from enterprise_research.application import (
    LangGraphCheckpointReader,
    ResearchGraphCheckpointBinding,
    ResearchRunApplicationService,
    ResearchRunExecutor,
    ResearchStageOutcome,
)
from enterprise_research.application.checkpoint_consistency import ResearchCheckpointObservation
from enterprise_research.application.safe_resume import ResearchResumeDisposition, ResearchSafeResumeInspector
from enterprise_research.domain import ResearchAggregate, ResearchRun, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    ResearchRunRow,
    ResearchTaskRow,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.document_repository import PostgresRetrievedDocumentRepository
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

pytestmark = pytest.mark.skipif(
    os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
    or os.getenv("ENTERPRISE_RESEARCH_PROCESS_CRASH_TESTS") != "1"
    or not os.getenv("ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN"),
    reason="5A SIGKILL requires explicit opt-in and independent local business/checkpoint test databases.",
)


async def verify_local_test_databases() -> str:
    """Reject remote hosts and any database other than the declared test DB."""
    from psycopg import AsyncConnection

    settings = PostgresSettings.from_env()
    dsn = os.environ["ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN"]
    parsed = urlsplit(dsn)
    checkpoint_name = unquote(parsed.path.lstrip("/"))
    if (
        settings.host not in {"localhost", "127.0.0.1", "::1"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.scheme not in {"postgres", "postgresql"}
        or checkpoint_name != "enterprise_graph_checkpoints_test"
        or settings.database == checkpoint_name
    ):
        raise RuntimeError("5A requires distinct local databases and enterprise_graph_checkpoints_test.")
    async with await AsyncConnection.connect(dsn) as conn:
        result = await conn.execute("SELECT current_database()")
        actual_name = (await result.fetchone())[0]
    if actual_name != checkpoint_name:
        raise RuntimeError("Checkpoint connection resolves to the wrong database.")
    return dsn


def kill_own_worker_after_barrier(mode: str, task_id, run_id) -> None:
    """Kill only a newly created child, AFTER its durable-write barrier."""
    worker_path = Path(__file__).with_name("_process_crash_worker.py")
    marker = f"FAULT_BARRIER:{mode}:{run_id}".encode()
    child = subprocess.Popen(
        [sys.executable, "-u", str(worker_path), mode, str(task_id), str(run_id)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    try:
        assert child.stdout is not None
        observed = b""
        deadline = time.monotonic() + 20
        with selectors.DefaultSelector() as poller:
            poller.register(child.stdout, selectors.EVENT_READ)
            while marker not in observed.splitlines() and time.monotonic() < deadline:
                events = poller.select(max(0, deadline - time.monotonic()))
                if not events:
                    break
                packet = os.read(child.stdout.fileno(), 4096)
                if not packet:
                    break
                observed = (observed + packet)[-32000:]
        if marker not in observed.splitlines():
            output = observed.decode("utf-8", errors="replace")
            for secret in (
                os.getenv("ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN", ""),
                os.getenv("ENTERPRISE_RESEARCH_DB_PASSWORD", ""),
            ):
                if secret:
                    output = output.replace(secret, "[REDACTED]")
            raise AssertionError(f"Worker did not reach {mode} barrier; exit={child.poll()}; log={output[-500:]}")
        # The only SIGKILL target is this Popen child PID. Database remains up.
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


class BlockingNextStage:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()

    async def run_stage(self, _task_id, _run_id, stage, *, lease=None):
        assert lease is not None
        self.calls.append(stage)
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("mode", "journal_outcome", "has_plan", "has_checkpoint", "disposition"),
    [
        ("after_entry", ResearchStageOutcome.STARTED, False, False, ResearchResumeDisposition.PAUSED),
        ("after_artifact", ResearchStageOutcome.STARTED, True, False, ResearchResumeDisposition.PAUSED),
        ("after_graph", ResearchStageOutcome.STARTED, True, True, ResearchResumeDisposition.PAUSED),
        ("after_journal", ResearchStageOutcome.COMPLETED, True, False, ResearchResumeDisposition.READY),
        ("journal_without_artifact", ResearchStageOutcome.COMPLETED, False, False, ResearchResumeDisposition.PAUSED),
    ],
)
def test_killed_worker_never_grants_unproven_stage_replay(mode, journal_outcome, has_plan, has_checkpoint, disposition):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async def exercise():
        dsn = await verify_local_test_databases()
        engine = build_async_engine(PostgresSettings.from_env())
        sessions = build_session_factory(engine)
        aggregate = ResearchAggregate.create(f"5A isolated process crash {mode}")
        run = ResearchRun.create(aggregate.task.task_id)
        binding = ResearchGraphCheckpointBinding(run.task_id, run.run_id, ResearchRunStatus.PLANNING, 1)
        saved = False
        try:
            research = PostgresResearchRepository(sessions)
            repository = PostgresResearchRunRepository(sessions)
            plans = PostgresResearchPlanRepository(sessions)
            documents = PostgresRetrievedDocumentRepository(sessions)
            await research.save(aggregate)
            saved = True
            await repository.save(run)

            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                await saver.setup()
            # No async DB connection is shared with the child process.
            await asyncio.to_thread(kill_own_worker_after_barrier, mode, run.task_id, run.run_id)

            # New saver/DB sessions after SIGKILL: no process-local state reused.
            fresh_runs_repo = PostgresResearchRunRepository(sessions)
            fresh_research = PostgresResearchRepository(sessions)
            fresh_runs = ResearchRunApplicationService(fresh_research, fresh_runs_repo)
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                reader = LangGraphCheckpointReader(saver)
                inspector = ResearchSafeResumeInspector(
                    fresh_runs, fresh_research, plans, documents, graph_checkpoints=reader
                )
                persisted = await fresh_runs.get_run(run.task_id, run.run_id)
                assert persisted.status == ResearchRunStatus.PLANNING
                async with sessions() as session:
                    run_row = await session.get(ResearchRunRow, run.run_id)
                    assert run_row is not None and run_row.lease_owner_id is not None
                attempts = await fresh_runs.inspect_stage_attempts(run.task_id, run.run_id)
                assert len(attempts) == 1
                assert attempts[0].fencing_token == 1
                assert attempts[0].outcome == journal_outcome
                assert ((await plans.get_for_run(run.run_id)) is not None) is has_plan
                assert ((await reader.latest(binding)) is not None) is has_checkpoint

                decision = await inspector.inspect(run.task_id, run.run_id)
                assert decision.disposition == disposition
                assert decision.next_stage == (
                    ResearchRunStatus.SEARCHING if disposition == ResearchResumeDisposition.READY else None
                )
                assert decision.checkpoint_reconciliations[0].checkpoint == (
                    ResearchCheckpointObservation.PRESENT if has_checkpoint else ResearchCheckpointObservation.ABSENT
                )

                runner = BlockingNextStage()
                executor = ResearchRunExecutor(fresh_runs, runner, resume_inspector=inspector, lease_ttl_seconds=2)
                try:
                    if disposition == ResearchResumeDisposition.PAUSED:
                        assert not await executor.resume(run.task_id, run.run_id)
                        assert runner.calls == []
                        assert len(await fresh_runs.inspect_stage_attempts(run.task_id, run.run_id)) == 1
                    else:
                        # SIGKILL leaves a lease until PostgreSQL clock expiry.
                        # The first attempt may already occur after expiry on
                        # a slow CI machine, so poll instead of assuming timing.
                        async def claim_after_expiry():
                            deadline = asyncio.get_running_loop().time() + 6
                            while asyncio.get_running_loop().time() < deadline:
                                if await executor.resume(run.task_id, run.run_id):
                                    return True
                                await asyncio.sleep(0.1)
                            return False

                        assert await claim_after_expiry()
                        await asyncio.wait_for(runner.entered.wait(), timeout=3)
                        assert runner.calls == [ResearchRunStatus.SEARCHING]
                        attempts = await fresh_runs.inspect_stage_attempts(run.task_id, run.run_id)
                        assert len([a for a in attempts if a.stage == ResearchRunStatus.PLANNING]) == 1
                        assert len([a for a in attempts if a.stage == ResearchRunStatus.SEARCHING]) == 1
                finally:
                    await executor.shutdown()
        finally:
            # Cleanup only our UUID Task and deterministic 5A Graph Thread.
            # No DROP DATABASE / global checkpoint cleanup / service termination.
            try:
                async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                    await saver.adelete_thread(binding.thread_id)
            finally:
                if saved:
                    async with sessions() as session:
                        async with session.begin():
                            await session.execute(delete(ResearchTaskRow).where(ResearchTaskRow.id == run.task_id))
                await engine.dispose()

    asyncio.run(exercise())
