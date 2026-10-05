from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from enterprise_research.domain import (
    ResearchRun,
    ResearchTask,
)
from enterprise_research.infrastructure.planning import (
    OpenAICompatiblePlannerSettings,
    OpenAICompatibleResearchPlanner,
    ResearchPlannerAdapterError,
)


def create_planner(
    handler: httpx.MockTransport,
) -> tuple[
    OpenAICompatibleResearchPlanner,
    httpx.AsyncClient,
]:
    client = httpx.AsyncClient(transport=handler)

    settings = OpenAICompatiblePlannerSettings(
        base_url="https://example.test/v1/",
        api_key="secret",
        model="glm-4",
    )

    return (
        OpenAICompatibleResearchPlanner(
            settings,
            client=client,
        ),
        client,
    )


def test_successful_plan_links_ids_and_request() -> None:
    async def run() -> None:
        seen: dict[str, object] = {}

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]

            body = json.loads(request.content)

            seen["body"] = body

            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "objective": ("Assess durable execution"),
                                        "questions": [
                                            {
                                                "question": ("How does checkpointing work?"),
                                                "rationale": ("Establish recovery semantics."),
                                            }
                                        ],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="LangGraph durable execution")

        research_run = ResearchRun(task_id=task.task_id)

        try:
            plan = await planner.plan(
                task=task,
                run=research_run,
            )
        finally:
            await client.aclose()

        assert plan.task_id == task.task_id
        assert plan.run_id == research_run.run_id

        assert plan.objective == ("Assess durable execution")

        assert plan.questions[0].question == "How does checkpointing work?"

        assert seen["url"] == ("https://example.test/v1/chat/completions")

        assert seen["auth"] == "Bearer secret"

        request_body = seen["body"]

        assert isinstance(
            request_body,
            dict,
        )

        assert request_body["model"] == "glm-4"
        assert request_body["stream"] is False

        user_payload = json.loads(request_body["messages"][1]["content"])

        assert user_payload["research_query"] == task.query

    asyncio.run(run())


def test_json_fence_is_accepted() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '```json\n{"objective":"O","questions":[{"question":"Q?","rationale":null}]}\n```'
                                )
                            }
                        }
                    ]
                },
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="Q")
        research_run = ResearchRun(task_id=task.task_id)

        try:
            plan = await planner.plan(
                task=task,
                run=research_run,
            )
        finally:
            await client.aclose()

        assert plan.objective == "O"

    asyncio.run(run())


def test_invalid_json_is_rejected() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not json"}}]},
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="Q")
        research_run = ResearchRun(task_id=task.task_id)

        try:
            with pytest.raises(
                ResearchPlannerAdapterError,
                match="invalid JSON",
            ):
                await planner.plan(
                    task=task,
                    run=research_run,
                )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_invalid_schema_is_rejected() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ('{"objective":"O","questions":[]}')}}]},
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="Q")
        research_run = ResearchRun(task_id=task.task_id)

        try:
            with pytest.raises(
                ResearchPlannerAdapterError,
                match="invalid research plan",
            ):
                await planner.plan(
                    task=task,
                    run=research_run,
                )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_http_failure_is_wrapped() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": "bad token"},
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="Q")
        research_run = ResearchRun(task_id=task.task_id)

        try:
            with pytest.raises(
                ResearchPlannerAdapterError,
                match="request failed",
            ):
                await planner.plan(
                    task=task,
                    run=research_run,
                )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_missing_content_is_rejected() -> None:
    async def run() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": []},
            )

        planner, client = create_planner(httpx.MockTransport(handler))

        task = ResearchTask(query="Q")
        research_run = ResearchRun(task_id=task.task_id)

        try:
            with pytest.raises(
                ResearchPlannerAdapterError,
                match="missing message content",
            ):
                await planner.plan(
                    task=task,
                    run=research_run,
                )
        finally:
            await client.aclose()

    asyncio.run(run())
