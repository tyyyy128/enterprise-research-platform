"""PostgreSQL repository for structured research plans."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import ResearchRunRepository
from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
    ResearchRunStatus,
)
from enterprise_research.infrastructure.postgres.fenced_writes import lock_fenced_stage
from enterprise_research.infrastructure.postgres.models import (
    ResearchPlanQuestionRow,
    ResearchPlanRow,
    ResearchRunRow,
)


class PostgresResearchPlanRepository:
    """Persist structured research plans in PostgreSQL."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def save(self, plan: ResearchPlan) -> None:
        """Legacy/unfenced plan bootstrap."""
        await self._save(plan, lease=None)

    async def save_guarded(
        self,
        plan: ResearchPlan,
        lease: ResearchRunLease,
        *,
        run_repository: ResearchRunRepository,
    ) -> None:
        """Fenced plan save sharing the run-row lock and business transaction."""
        await self._save(plan, lease=lease)

    async def _save(self, plan: ResearchPlan, *, lease: ResearchRunLease | None) -> None:
        """Insert or replace one run's structured plan."""
        async with self._session_factory() as session:
            async with session.begin():
                if lease is not None:
                    if lease.run_id != plan.run_id:
                        raise ValueError("Plan and lease run IDs differ.")
                    await lock_fenced_stage(session, lease, task_id=plan.task_id, stage=ResearchRunStatus.PLANNING)
                run_row = await session.get(
                    ResearchRunRow,
                    plan.run_id,
                )

                if run_row is None:
                    raise ValueError("Research plan references an unknown run.")

                if run_row.task_id != plan.task_id:
                    raise ValueError("Research plan task_id does not match its research run.")

                existing = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == plan.run_id))

                if existing is None:
                    session.add(
                        ResearchPlanRow(
                            id=plan.plan_id,
                            run_id=plan.run_id,
                            objective=plan.objective,
                            created_at=plan.created_at,
                        )
                    )

                else:
                    if existing.id != plan.plan_id:
                        raise ValueError("Research run already has a different plan.")

                    existing.objective = plan.objective
                    existing.created_at = plan.created_at

                    await session.execute(
                        delete(ResearchPlanQuestionRow).where(ResearchPlanQuestionRow.plan_id == plan.plan_id)
                    )

                await session.flush()

                session.add_all(
                    [
                        ResearchPlanQuestionRow(
                            id=question.question_id,
                            plan_id=plan.plan_id,
                            position=position,
                            question=question.question,
                            rationale=question.rationale,
                        )
                        for position, question in enumerate(plan.questions)
                    ]
                )

    async def get_for_run(
        self,
        run_id: UUID,
    ) -> ResearchPlan | None:
        """Load one structured plan by research run."""
        async with self._session_factory() as session:
            plan_row = await session.scalar(select(ResearchPlanRow).where(ResearchPlanRow.run_id == run_id))

            if plan_row is None:
                return None

            run_row = await session.get(
                ResearchRunRow,
                plan_row.run_id,
            )

            if run_row is None:
                raise RuntimeError("Persisted research plan references a missing research run.")

            result = await session.scalars(
                select(ResearchPlanQuestionRow)
                .where(ResearchPlanQuestionRow.plan_id == plan_row.id)
                .order_by(ResearchPlanQuestionRow.position)
            )

            question_rows = list(result)

            return ResearchPlan(
                plan_id=plan_row.id,
                task_id=run_row.task_id,
                run_id=plan_row.run_id,
                objective=plan_row.objective,
                questions=[
                    ResearchQuestion(
                        question_id=row.id,
                        question=row.question,
                        rationale=row.rationale,
                    )
                    for row in question_rows
                ],
                created_at=plan_row.created_at,
            )
