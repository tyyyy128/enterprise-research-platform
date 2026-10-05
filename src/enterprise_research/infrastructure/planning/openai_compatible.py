"""OpenAI-compatible LLM adapter for structured research planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterprise_research.domain import (
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchTask,
)

_SYSTEM_PROMPT = """You are an enterprise research planner.

Do not answer the research question itself.

Break the request into focused, non-overlapping questions that can be
answered with evidence.

Prefer questions that can be supported by primary or authoritative
sources.

Return only one JSON object with exactly this shape:

{
  "objective": "clear research objective",
  "questions": [
    {
      "question": "one focused research question",
      "rationale": "why this question matters"
    }
  ]
}

Do not use Markdown fences or add commentary outside the JSON object.
"""


class ResearchPlannerAdapterError(RuntimeError):
    """Raised when an LLM planner response cannot be used safely."""


@dataclass(frozen=True, slots=True)
class OpenAICompatiblePlannerSettings:
    """Connection settings for an OpenAI-compatible planner."""

    base_url: str
    api_key: str
    model: str
    temperature: float = 0.1
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in (
            "base_url",
            "api_key",
            "model",
        ):
            value = getattr(self, name)

            if not value.strip():
                raise ValueError(f"{name} must not be blank.")

        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0.")

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

    @property
    def chat_completions_url(self) -> str:
        """Return the compatible chat-completions endpoint."""
        return f"{self.base_url.rstrip('/')}/chat/completions"


class _PlannerQuestionPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    question: str = Field(min_length=1)
    rationale: str | None = Field(
        default=None,
        min_length=1,
    )


class _PlannerPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    objective: str = Field(min_length=1)

    questions: list[_PlannerQuestionPayload] = Field(min_length=1)


class OpenAICompatibleResearchPlanner:
    """Generate ResearchPlan objects through a chat endpoint."""

    def __init__(
        self,
        settings: OpenAICompatiblePlannerSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def plan(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        """Generate and validate one structured research plan."""
        if self._client is not None:
            return await self._plan_with_client(
                self._client,
                task=task,
                run=run,
            )

        async with httpx.AsyncClient(timeout=self._settings.timeout_seconds) as client:
            return await self._plan_with_client(
                client,
                task=task,
                run=run,
            )

    async def _plan_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        task: ResearchTask,
        run: ResearchRun,
    ) -> ResearchPlan:
        request_body = {
            "model": self._settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "research_query": task.query,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": self._settings.temperature,
            "stream": False,
        }

        try:
            response = await client.post(
                self._settings.chat_completions_url,
                headers={
                    "Authorization": (f"Bearer {self._settings.api_key}"),
                    "Content-Type": "application/json",
                },
                json=request_body,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise ResearchPlannerAdapterError("Planner model request failed.") from exc

        content = self._extract_content(response)
        payload = self._parse_payload(content)

        return ResearchPlan(
            task_id=task.task_id,
            run_id=run.run_id,
            objective=payload.objective,
            questions=[
                ResearchQuestion(
                    question=item.question,
                    rationale=item.rationale,
                )
                for item in payload.questions
            ],
        )

    @staticmethod
    def _extract_content(
        response: httpx.Response,
    ) -> str:
        try:
            body: Any = response.json()

            content = body["choices"][0]["message"]["content"]

        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise ResearchPlannerAdapterError("Planner model response is missing message content.") from exc

        if not isinstance(content, str) or not content.strip():
            raise ResearchPlannerAdapterError("Planner model returned empty message content.")

        return content.strip()

    @staticmethod
    def _parse_payload(
        content: str,
    ) -> _PlannerPayload:
        normalized = _strip_optional_json_fence(content)

        try:
            raw = json.loads(normalized)

        except JSONDecodeError as exc:
            raise ResearchPlannerAdapterError("Planner model returned invalid JSON.") from exc

        try:
            return _PlannerPayload.model_validate(raw)

        except ValidationError as exc:
            raise ResearchPlannerAdapterError("Planner model returned an invalid research plan.") from exc


def _strip_optional_json_fence(
    content: str,
) -> str:
    """Accept a JSON code fence while still preferring raw JSON."""
    text = content.strip()

    if not text.startswith("```"):
        return text

    lines = text.splitlines()

    if len(lines) < 3 or lines[-1].strip() != "```":
        return text

    opening = lines[0].strip().casefold()

    if opening not in {
        "```",
        "```json",
    }:
        return text

    return "\n".join(lines[1:-1]).strip()
