"""Application service for turning research questions into sources."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from enterprise_research.application.errors import (
    ResearchRunNotFoundError,
    ResearchSearchError,
    ResearchTaskNotFoundError,
)
from enterprise_research.application.plan_repository import (
    ResearchPlanRepository,
)
from enterprise_research.application.repository import (
    ResearchRepository,
)
from enterprise_research.application.run_lease import ResearchRunLease
from enterprise_research.application.run_repository import (
    ResearchRunRepository,
)
from enterprise_research.application.search import (
    SearchProvider,
    SearchResult,
)
from enterprise_research.domain import (
    ResearchRunStatus,
    Source,
    SourceType,
)

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
}


def canonicalize_url(url: str) -> str:
    """Normalize a web URL for deterministic de-duplication."""
    parsed = urlsplit(url)

    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()

    if not scheme or not host:
        raise ValueError(f"URL must be absolute: {url!r}")

    port = parsed.port

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)

    netloc = host if port is None or default_port else f"{host}:{port}"

    path = parsed.path or "/"

    if path != "/":
        path = path.rstrip("/") or "/"

    query_pairs: list[tuple[str, str]] = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized_key = key.casefold()

        if normalized_key.startswith("utm_"):
            continue

        if normalized_key in _TRACKING_QUERY_KEYS:
            continue

        query_pairs.append((key, value))

    query_pairs.sort(
        key=lambda item: (
            item[0].casefold(),
            item[1],
        )
    )

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            urlencode(
                query_pairs,
                doseq=True,
            ),
            "",
        )
    )


class ResearchSearchService:
    """Search every planned question and persist unique web sources."""

    def __init__(
        self,
        research_repository: ResearchRepository,
        run_repository: ResearchRunRepository,
        plan_repository: ResearchPlanRepository,
        provider: SearchProvider,
        *,
        max_results_per_question: int = 5,
    ) -> None:
        if max_results_per_question < 1:
            raise ValueError("max_results_per_question must be at least 1")

        self._research_repository = research_repository
        self._run_repository = run_repository
        self._plan_repository = plan_repository
        self._provider = provider
        self._max_results_per_question = max_results_per_question

    async def search_run(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        lease: ResearchRunLease | None = None,
    ) -> list[Source]:
        """Search all plan questions and persist de-duplicated sources."""
        aggregate = await self._research_repository.get(task_id)

        if aggregate is None:
            raise ResearchTaskNotFoundError(f"Research task not found: {task_id}")

        run = await self._run_repository.get(run_id)

        if run is None or run.task_id != task_id:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}")

        if run.status != ResearchRunStatus.SEARCHING:
            raise ResearchSearchError("Research run must be in 'searching' state before searching.")

        plan = await self._plan_repository.get_for_run(run_id)

        if plan is None:
            raise ResearchSearchError(f"Research plan not found for run: {run_id}")

        if plan.task_id != task_id or plan.run_id != run_id:
            raise ResearchSearchError("Persisted research plan belongs to a different task or run.")

        source_id_by_url: dict[str, UUID] = {}

        for source in aggregate.sources.values():
            if source.url is None:
                continue

            canonical_url = canonicalize_url(str(source.url))

            source_id_by_url.setdefault(
                canonical_url,
                source.source_id,
            )

        touched_source_ids: list[UUID] = []
        touched_seen: set[UUID] = set()
        changed = False

        for question in plan.questions:
            query = question.question

            try:
                results = await self._provider.search(
                    query,
                    max_results=(self._max_results_per_question),
                )
            except Exception as exc:
                raise ResearchSearchError(
                    f"Search provider failed for research question {question.question_id}: {question.question}"
                ) from exc

            for result in list(results)[: self._max_results_per_question]:
                canonical_url = canonicalize_url(str(result.url))

                source_id = source_id_by_url.get(canonical_url)

                if source_id is None:
                    source = Source(
                        source_type=SourceType.WEB,
                        title=result.title,
                        url=result.url,
                        metadata={},
                    )

                    aggregate.add_source(source)

                    source_id = source.source_id

                    source_id_by_url[canonical_url] = source_id

                    changed = True

                else:
                    source = aggregate.sources[source_id]

                (
                    updated_source,
                    metadata_changed,
                ) = _merge_search_hit(
                    source,
                    result=result,
                    canonical_url=canonical_url,
                    run_id=run_id,
                    question_id=question.question_id,
                    question=question.question,
                    query=query,
                )

                if metadata_changed:
                    aggregate.sources[source_id] = updated_source

                    changed = True

                if source_id not in touched_seen:
                    touched_seen.add(source_id)

                    touched_source_ids.append(source_id)

        if changed:
            if lease is None:
                await self._research_repository.save(aggregate)
            else:
                await self._research_repository.save_guarded(
                    aggregate,
                    lease,
                    ResearchRunStatus.SEARCHING,
                    run_repository=self._run_repository,
                )

        return [aggregate.sources[source_id].model_copy(deep=True) for source_id in touched_source_ids]


def _merge_search_hit(
    source: Source,
    *,
    result: SearchResult,
    canonical_url: str,
    run_id: UUID,
    question_id: UUID,
    question: str,
    query: str,
) -> tuple[Source, bool]:
    metadata = deepcopy(source.metadata)

    raw_search = metadata.get("search")

    search_metadata = dict(raw_search) if isinstance(raw_search, dict) else {}

    raw_hits = search_metadata.get("hits")

    hits = list(raw_hits) if isinstance(raw_hits, list) else []

    hit = {
        "run_id": str(run_id),
        "question_id": str(question_id),
        "question": question,
        "query": query,
        "provider": result.provider,
        "score": result.score,
        "snippet": result.snippet,
    }

    dedupe_key = (
        hit["run_id"],
        hit["question_id"],
        hit["query"],
        hit["provider"],
    )

    existing_keys = {
        (
            item.get("run_id"),
            item.get("question_id"),
            item.get("query"),
            item.get("provider"),
        )
        for item in hits
        if isinstance(item, dict)
    }

    changed = False

    if dedupe_key not in existing_keys:
        hits.append(hit)
        changed = True

    if search_metadata.get("canonical_url") != canonical_url:
        search_metadata["canonical_url"] = canonical_url

        changed = True

    if search_metadata.get("hits") != hits:
        search_metadata["hits"] = hits

        changed = True

    if metadata.get("search") != search_metadata:
        metadata["search"] = search_metadata

        changed = True

    if not changed:
        return source, False

    return (
        source.model_copy(
            update={
                "metadata": metadata,
            },
            deep=True,
        ),
        True,
    )
