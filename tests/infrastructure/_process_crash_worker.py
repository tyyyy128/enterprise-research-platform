"""Private 5A fault worker; only the opt-in integration test may launch it.

The parent uses SIGKILL after receiving the barrier printed *after* a durable
DB write. No database service or unrelated process is ever terminated here.
Do not invoke this helper against production databases.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from enterprise_research.application import (
    ResearchLangGraphStageRunner,
    ResearchRunApplicationService,
    ResearchStageOutcome,
)
from enterprise_research.domain import ResearchPlan, ResearchQuestion, ResearchRunStatus
from enterprise_research.infrastructure.postgres import (
    PostgresResearchRepository,
    PostgresResearchRunRepository,
    PostgresSettings,
    build_async_engine,
    build_session_factory,
)
from enterprise_research.infrastructure.postgres.plan_repository import PostgresResearchPlanRepository

MODES = {
    "after_entry",
    "after_artifact",
    "after_graph",
    "after_journal",
    "journal_without_artifact",
}


async def main(mode: str, task_id: UUID, run_id: UUID) -> None:
    if (
        os.getenv("ENTERPRISE_RESEARCH_RUN_DB_TESTS") != "1"
        or os.getenv("ENTERPRISE_RESEARCH_PROCESS_CRASH_TESTS") != "1"
    ):
        raise RuntimeError("This worker requires the explicit 5A test opt-in flags.")
    if mode not in MODES:
        raise ValueError("Unknown injected fault boundary.")

    settings = PostgresSettings.from_env()
    if settings.host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("SIGKILL fault injection requires local PostgreSQL.")
    checkpoint_dsn = os.getenv("ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN")
    if not checkpoint_dsn:
        raise RuntimeError("A separate checkpoint test DSN is required.")
    checkpoint_url = urlsplit(checkpoint_dsn)
    if (
        checkpoint_url.scheme not in {"postgres", "postgresql"}
        or checkpoint_url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or unquote(checkpoint_url.path.lstrip("/")) != "enterprise_graph_checkpoints_test"
        or settings.database == "enterprise_graph_checkpoints_test"
    ):
        raise RuntimeError("The fault worker requires two distinct local test databases.")

    engine = build_async_engine(settings)
    sessions = build_session_factory(engine)
    try:
        runs_repo = PostgresResearchRunRepository(sessions)
        app = ResearchRunApplicationService(PostgresResearchRepository(sessions), runs_repo)
        plans = PostgresResearchPlanRepository(sessions)
        lease = await app.claim_run_lease(task_id, run_id, uuid4(), ttl_seconds=2)
        if lease is None:
            raise RuntimeError("Failed to claim this test run for the fault worker.")
        await app.advance_run(task_id, run_id, ResearchRunStatus.PLANNING, lease=lease)
        await app.start_stage(task_id, run_id, ResearchRunStatus.PLANNING, lease=lease)

        async def write_plan():
            await plans.save_guarded(
                ResearchPlan(
                    task_id=task_id,
                    run_id=run_id,
                    objective="5A: kill the worker after a committed planning artifact",
                    questions=[ResearchQuestion(question="Was the side effect committed?")],
                ),
                lease,
                run_repository=runs_repo,
            )

        if mode == "after_graph":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            class PlanStage:
                async def run_stage(self, _task_id, _run_id, stage, *, lease=None):
                    assert stage == ResearchRunStatus.PLANNING
                    assert lease is not None
                    await write_plan()

            async with AsyncPostgresSaver.from_conn_string(
                os.environ["ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN"]
            ) as saver:
                await ResearchLangGraphStageRunner(PlanStage(), saver).run_stage(
                    task_id, run_id, ResearchRunStatus.PLANNING, lease=lease
                )
        elif mode in {"after_artifact", "after_journal"}:
            await write_plan()

        if mode in {"after_journal", "journal_without_artifact"}:
            await app.finish_stage(
                task_id, run_id, ResearchRunStatus.PLANNING, lease=lease, outcome=ResearchStageOutcome.COMPLETED
            )

        # The test parent will kill only this child after all writes above
        # have returned. Never release the lease or finish the stage here.
        print(f"FAULT_BARRIER:{mode}:{run_id}", flush=True)
        await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Usage: _process_crash_worker.py MODE TASK_UUID RUN_UUID")
    asyncio.run(main(sys.argv[1], UUID(sys.argv[2]), UUID(sys.argv[3])))
