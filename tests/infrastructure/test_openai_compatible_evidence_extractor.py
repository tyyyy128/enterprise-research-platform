from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from enterprise_research.application import (
    DocumentChunk,
)
from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchTask,
    Source,
    SourceType,
)
from enterprise_research.infrastructure.evidence_extraction import (
    EvidenceExtractorAdapterError,
    OpenAICompatibleEvidenceExtractor,
    OpenAICompatibleEvidenceExtractorSettings,
)


def build_inputs() -> tuple[
    ResearchTask,
    ResearchRun,
    ResearchPlan,
    Source,
    DocumentChunk,
]:
    task = ResearchTask(query="Research durable execution")

    run = ResearchRun(task_id=task.task_id)

    plan = ResearchPlan(
        task_id=task.task_id,
        run_id=run.run_id,
        objective="Assess reliability",
        questions=[
            ResearchQuestion(
                question=("How does recovery work?"),
                rationale=("Assess recovery."),
            )
        ],
    )

    source = Source(
        source_type=SourceType.WEB,
        title="Example",
        url="https://example.com/a",
    )

    chunk = DocumentChunk(
        index=0,
        start_offset=100,
        end_offset=111,
        content="hello world",
    )

    return (
        task,
        run,
        plan,
        source,
        chunk,
    )


def create_extractor(
    transport: httpx.MockTransport,
    **overrides: object,
) -> tuple[
    OpenAICompatibleEvidenceExtractor,
    httpx.AsyncClient,
]:
    client = httpx.AsyncClient(transport=transport)

    settings = OpenAICompatibleEvidenceExtractorSettings(
        base_url=("https://example.test/v1/"),
        api_key="secret",
        model="glm-4",
        **overrides,
    )

    return (
        OpenAICompatibleEvidenceExtractor(
            settings,
            client=client,
        ),
        client,
    )


def test_success_preserves_exact_excerpt_and_request_context() -> None:
    async def run_test() -> None:
        seen: dict[
            str,
            object,
        ] = {}

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen["url"] = str(request.url)

            seen["auth"] = request.headers["Authorization"]

            seen["body"] = json.loads(request.content)

            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    json.dumps(
                                        {
                                            "evidence": [
                                                {
                                                    "content": ("  exact  "),
                                                    "relevance_score": (0.95),
                                                }
                                            ]
                                        }
                                    )
                                )
                            }
                        }
                    ]
                },
            )

        extractor, client = create_extractor(httpx.MockTransport(handler))

        (
            task,
            run,
            plan,
            source,
            chunk,
        ) = build_inputs()

        try:
            candidates = await extractor.extract(
                task=task,
                run=run,
                plan=plan,
                source=source,
                chunk=chunk,
            )

        finally:
            await client.aclose()

        assert candidates[0].content == "  exact  "

        assert seen["url"] == ("https://example.test/v1/chat/completions")

        assert seen["auth"] == "Bearer secret"

        body = seen["body"]

        assert isinstance(
            body,
            dict,
        )

        user_payload = json.loads(body["messages"][1]["content"])

        assert user_payload["research_query"] == task.query

        assert user_payload["chunk"]["content"] == chunk.content

        assert "year/column header" in body["messages"][0]["content"]
        assert "never stitch" in body["messages"][0]["content"].lower()
        assert body["temperature"] == 0.0

        assert body["stream"] is False

    asyncio.run(run_test())


def test_json_fence_and_empty_evidence_are_supported() -> None:
    async def run_test() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ('```json\n{"evidence":[]}\n```')}}]},
            )

        extractor, client = create_extractor(httpx.MockTransport(handler))

        (
            task,
            run,
            plan,
            source,
            chunk,
        ) = build_inputs()

        try:
            result = await extractor.extract(
                task=task,
                run=run,
                plan=plan,
                source=source,
                chunk=chunk,
            )

        finally:
            await client.aclose()

        assert result == []

    asyncio.run(run_test())


def test_invalid_json_is_rejected() -> None:
    async def run_test() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ("not json")}}]},
            )

        extractor, client = create_extractor(httpx.MockTransport(handler))

        args = build_inputs()

        try:
            with pytest.raises(
                EvidenceExtractorAdapterError,
                match="invalid JSON",
            ):
                await extractor.extract(
                    task=args[0],
                    run=args[1],
                    plan=args[2],
                    source=args[3],
                    chunk=args[4],
                )

        finally:
            await client.aclose()

    asyncio.run(run_test())


def test_http_error_is_wrapped() -> None:
    async def run_test() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": "bad key"},
            )

        extractor, client = create_extractor(httpx.MockTransport(handler))

        args = build_inputs()

        try:
            with pytest.raises(
                EvidenceExtractorAdapterError,
                match="request failed",
            ):
                await extractor.extract(
                    task=args[0],
                    run=args[1],
                    plan=args[2],
                    source=args[3],
                    chunk=args[4],
                )

        finally:
            await client.aclose()

    asyncio.run(run_test())


def test_missing_message_content_is_rejected() -> None:
    async def run_test() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": []},
            )

        extractor, client = create_extractor(httpx.MockTransport(handler))

        args = build_inputs()

        try:
            with pytest.raises(
                EvidenceExtractorAdapterError,
                match=("missing message content"),
            ):
                await extractor.extract(
                    task=args[0],
                    run=args[1],
                    plan=args[2],
                    source=args[3],
                    chunk=args[4],
                )

        finally:
            await client.aclose()

    asyncio.run(run_test())


def test_too_many_candidates_are_rejected() -> None:
    async def run_test() -> None:
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            items = [
                {
                    "content": f"e{i}",
                    "relevance_score": 0.5,
                }
                for i in range(3)
            ]

            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": (json.dumps({"evidence": (items)}))}}]},
            )

        extractor, client = create_extractor(
            httpx.MockTransport(handler),
            max_candidates_per_chunk=2,
        )

        args = build_inputs()

        try:
            with pytest.raises(
                EvidenceExtractorAdapterError,
                match="too many",
            ):
                await extractor.extract(
                    task=args[0],
                    run=args[1],
                    plan=args[2],
                    source=args[3],
                    chunk=args[4],
                )

        finally:
            await client.aclose()

    asyncio.run(run_test())


def test_too_long_excerpt_is_rejected() -> None:
    async def run_test() -> None:
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
                                    json.dumps(
                                        {
                                            "evidence": [
                                                {
                                                    "content": ("abcdef"),
                                                    "relevance_score": (0.5),
                                                }
                                            ]
                                        }
                                    )
                                )
                            }
                        }
                    ]
                },
            )

        extractor, client = create_extractor(
            httpx.MockTransport(handler),
            max_excerpt_chars=5,
        )

        args = build_inputs()

        try:
            with pytest.raises(
                EvidenceExtractorAdapterError,
                match="max_excerpt_chars",
            ):
                await extractor.extract(
                    task=args[0],
                    run=args[1],
                    plan=args[2],
                    source=args[3],
                    chunk=args[4],
                )

        finally:
            await client.aclose()

    asyncio.run(run_test())


def test_settings_validate_bounds() -> None:
    with pytest.raises(
        ValueError,
        match="max_candidates_per_chunk",
    ):
        OpenAICompatibleEvidenceExtractorSettings(
            base_url="https://example.test",
            api_key="secret",
            model="glm-4",
            max_candidates_per_chunk=0,
        )

    with pytest.raises(
        ValueError,
        match="max_excerpt_chars",
    ):
        OpenAICompatibleEvidenceExtractorSettings(
            base_url="https://example.test",
            api_key="secret",
            model="glm-4",
            max_excerpt_chars=0,
        )


def test_request_includes_configured_output_limits() -> None:
    async def run_test() -> None:
        seen: dict[
            str,
            object,
        ] = {}

        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            seen["body"] = json.loads(request.content)

            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": (json.dumps({"evidence": []}))}}]},
            )

        extractor, client = create_extractor(
            httpx.MockTransport(handler),
            max_candidates_per_chunk=3,
            max_excerpt_chars=321,
        )

        args = build_inputs()

        try:
            result = await extractor.extract(
                task=args[0],
                run=args[1],
                plan=args[2],
                source=args[3],
                chunk=args[4],
            )

        finally:
            await client.aclose()

        assert result == []

        body = seen["body"]

        assert isinstance(
            body,
            dict,
        )

        messages = body["messages"]

        assert isinstance(
            messages,
            list,
        )

        system_prompt = messages[0]["content"]

        assert "Return at most 3 evidence items." in system_prompt

        assert "Each evidence excerpt must be at most 321 characters." in system_prompt

    asyncio.run(run_test())
