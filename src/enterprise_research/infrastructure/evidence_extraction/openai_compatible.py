"""OpenAI-compatible adapter for extracting verbatim research evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from enterprise_research.application.evidence_extraction import (
    DocumentChunk,
    EvidenceCandidate,
)
from enterprise_research.domain import (
    ResearchPlan,
    ResearchRun,
    ResearchTask,
    Source,
)

_SYSTEM_PROMPT = """You extract evidence for enterprise research.

Return only verbatim excerpts copied exactly from the provided document chunk.
Do not paraphrase, summarize, fix grammar, or invent text.

Select only excerpts that materially help answer the research objective or one
of its research questions.

For exact financial figures, prefer one contiguous verbatim span that includes
both the year/column header, units (such as "In millions"), and the relevant
numbered row. Never stitch separate passages or silently convert units.
If the source chunk cannot support the specific requested figure, return [].

Return exactly one JSON object with this shape:

{
  "evidence": [
    {
      "content": "exact excerpt copied from the chunk",
      "relevance_score": 0.95
    }
  ]
}

Return an empty evidence array when the chunk contains no useful evidence.

Do not use Markdown fences or commentary outside the JSON object.
"""


class EvidenceExtractorAdapterError(RuntimeError):
    """Raised when an evidence model response cannot be used safely."""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEvidenceExtractorSettings:
    """Connection and output bounds for an evidence extractor."""

    base_url: str
    api_key: str
    model: str

    temperature: float = 0.0
    timeout_seconds: float = 60.0

    max_candidates_per_chunk: int = 6
    max_excerpt_chars: int = 2_000

    def __post_init__(self) -> None:
        for name in (
            "base_url",
            "api_key",
            "model",
        ):
            value = getattr(
                self,
                name,
            )

            if not value.strip():
                raise ValueError(f"{name} must not be blank.")

        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError("temperature must be between 0.0 and 2.0.")

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        if self.max_candidates_per_chunk < 1:
            raise ValueError("max_candidates_per_chunk must be at least 1.")

        if self.max_excerpt_chars < 1:
            raise ValueError("max_excerpt_chars must be at least 1.")

    @property
    def chat_completions_url(
        self,
    ) -> str:
        """Return compatible chat-completions endpoint."""
        return f"{self.base_url.rstrip('/')}/chat/completions"


class _EvidenceItemPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    content: str = Field(min_length=1)

    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
    )


class _EvidencePayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    evidence: list[_EvidenceItemPayload]


class OpenAICompatibleEvidenceExtractor:
    """Extract evidence candidates through chat completions."""

    def __init__(
        self,
        settings: (OpenAICompatibleEvidenceExtractorSettings),
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def extract(
        self,
        *,
        task: ResearchTask,
        run: ResearchRun,
        plan: ResearchPlan,
        source: Source,
        chunk: DocumentChunk,
    ) -> list[EvidenceCandidate]:
        """Return model-proposed verbatim evidence candidates."""
        if self._client is not None:
            return await self._extract_with_client(
                self._client,
                task=task,
                run=run,
                plan=plan,
                source=source,
                chunk=chunk,
            )

        async with httpx.AsyncClient(timeout=(self._settings.timeout_seconds)) as client:
            return await self._extract_with_client(
                client,
                task=task,
                run=run,
                plan=plan,
                source=source,
                chunk=chunk,
            )

    async def _extract_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        task: ResearchTask,
        run: ResearchRun,
        plan: ResearchPlan,
        source: Source,
        chunk: DocumentChunk,
    ) -> list[EvidenceCandidate]:
        request_body = {
            "model": self._settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        _SYSTEM_PROMPT
                        + "\n\nOutput limits:\n"
                        + (f"- Return at most {self._settings.max_candidates_per_chunk} evidence items.\n")
                        + (f"- Each evidence excerpt must be at most {self._settings.max_excerpt_chars} characters.")
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "research_query": (task.query),
                            "objective": (plan.objective),
                            "questions": [
                                {
                                    "question": (item.question),
                                    "rationale": (item.rationale),
                                }
                                for item in plan.questions
                            ],
                            "source": {
                                "title": (source.title),
                                "url": (str(source.url) if source.url is not None else None),
                            },
                            "chunk": {
                                "index": (chunk.index),
                                "start_offset": (chunk.start_offset),
                                "end_offset": (chunk.end_offset),
                                "content": (chunk.content),
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": (self._settings.temperature),
            "stream": False,
        }

        try:
            response = await client.post(
                self._settings.chat_completions_url,
                headers={
                    "Authorization": (f"Bearer {self._settings.api_key}"),
                    "Content-Type": ("application/json"),
                },
                json=request_body,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise EvidenceExtractorAdapterError("Evidence model request failed.") from exc

        content = self._extract_content(response)

        payload = self._parse_payload(content)

        if len(payload.evidence) > self._settings.max_candidates_per_chunk:
            raise EvidenceExtractorAdapterError("Evidence model returned too many candidates.")

        candidates: list[EvidenceCandidate] = []

        for item in payload.evidence:
            if len(item.content) > self._settings.max_excerpt_chars:
                raise EvidenceExtractorAdapterError(
                    "Evidence model returned an excerpt that exceeds max_excerpt_chars."
                )

            try:
                candidates.append(
                    EvidenceCandidate(
                        content=item.content,
                        relevance_score=(item.relevance_score),
                    )
                )

            except ValidationError as exc:
                raise (EvidenceExtractorAdapterError("Evidence model returned an invalid candidate.")) from exc

        return candidates

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
            raise EvidenceExtractorAdapterError("Evidence model response is missing message content.") from exc

        if not isinstance(content, str) or not content.strip():
            raise EvidenceExtractorAdapterError("Evidence model returned empty message content.")

        return content.strip()

    @staticmethod
    def _parse_payload(
        content: str,
    ) -> _EvidencePayload:
        normalized = _strip_optional_json_fence(content)

        try:
            raw = json.loads(normalized)

        except JSONDecodeError as exc:
            raise EvidenceExtractorAdapterError("Evidence model returned invalid JSON.") from exc

        try:
            return _EvidencePayload.model_validate(raw)

        except ValidationError as exc:
            raise EvidenceExtractorAdapterError("Evidence model returned an invalid evidence payload.") from exc


def _strip_optional_json_fence(
    content: str,
) -> str:
    """Accept JSON fences while preferring raw JSON."""
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
