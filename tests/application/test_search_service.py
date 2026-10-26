from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import uuid4

import pytest

from enterprise_research.application import (
    ResearchSearchError,
    ResearchSearchService,
    ResearchTaskNotFoundError,
    SearchResult,
    canonicalize_url,
)
from enterprise_research.domain import (
    ResearchAggregate,
    ResearchPlan,
    ResearchQuestion,
    ResearchRun,
    ResearchRunStatus,
    Source,
    SourceType,
)
from enterprise_research.infrastructure import (
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
)
from enterprise_research.infrastructure.memory_plan_repository import (
    InMemoryResearchPlanRepository,
)


class RecordingProvider:
    def __init__(
        self,
        responses: dict[
            str,
            Sequence[SearchResult],
        ],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> Sequence[SearchResult]:
        self.calls.append(
            (
                query,
                max_results,
            )
        )

        return self.responses.get(
            query,
            [],
        )


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> Sequence[SearchResult]:
        self.calls += 1

        raise RuntimeError("search unavailable")


async def setup_searching_run() -> tuple[
    InMemoryResearchRepository,
    InMemoryResearchRunRepository,
    InMemoryResearchPlanRepository,
    ResearchAggregate,
    ResearchRun,
    ResearchPlan,
]:
    research_repository = InMemoryResearchRepository()

    run_repository = InMemoryResearchRunRepository()

    plan_repository = InMemoryResearchPlanRepository()

    aggregate = ResearchAggregate.create("Evaluate durable execution")

    await research_repository.save(aggregate)

    run = ResearchRun.create(aggregate.task.task_id)

    run.advance(ResearchRunStatus.PLANNING)

    run.advance(ResearchRunStatus.SEARCHING)

    await run_repository.save(run)

    plan = ResearchPlan(
        task_id=aggregate.task.task_id,
        run_id=run.run_id,
        objective=("Evaluate durable execution"),
        questions=[
            ResearchQuestion(question=("How reliable is durable execution?")),
            ResearchQuestion(question=("How does recovery work?")),
        ],
    )

    await plan_repository.save(plan)

    return (
        research_repository,
        run_repository,
        plan_repository,
        aggregate,
        run,
        plan,
    )


def test_canonicalize_url_removes_tracking_fragment_and_default_port() -> None:
    assert canonicalize_url("HTTPS://Example.COM:443/docs/?b=2&utm_source=x&a=1#section") == (
        "https://example.com/docs?a=1&b=2"
    )


def test_searches_every_question_and_persists_unique_sources() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            aggregate,
            run,
            plan,
        ) = await setup_searching_run()

        first_query = plan.questions[0].question

        second_query = plan.questions[1].question

        provider = RecordingProvider(
            {
                first_query: [
                    SearchResult(
                        title=("LangGraph docs"),
                        url=("https://example.com/langgraph?utm_source=search"),
                        snippet=("Durable execution docs"),
                        score=0.95,
                        provider="fake",
                    ),
                    SearchResult(
                        title=("Architecture guide"),
                        url=("https://example.com/architecture"),
                        snippet="Architecture",
                        score=0.80,
                        provider="fake",
                    ),
                ],
                second_query: [
                    SearchResult(
                        title=("LangGraph docs duplicate"),
                        url=("https://EXAMPLE.com/langgraph#recovery"),
                        snippet=("Recovery docs"),
                        score=0.91,
                        provider="fake",
                    ),
                    SearchResult(
                        title="Recovery guide",
                        url=("https://example.com/recovery/"),
                        snippet="Recovery",
                        score=0.88,
                        provider="fake",
                    ),
                ],
            }
        )

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            provider,
            max_results_per_question=5,
        )

        touched = await service.search_run(
            aggregate.task.task_id,
            run.run_id,
        )

        stored = await research_repository.get(aggregate.task.task_id)

        assert stored is not None

        assert provider.calls == [
            (
                first_query,
                5,
            ),
            (
                second_query,
                5,
            ),
        ]

        assert len(stored.sources) == 3

        assert len(touched) == 3

        langgraph = next(
            source
            for source in stored.sources.values()
            if canonicalize_url(str(source.url)) == ("https://example.com/langgraph")
        )

        hits = langgraph.metadata["search"]["hits"]

        assert len(hits) == 2

        assert {hit["question_id"] for hit in hits} == {
            str(plan.questions[0].question_id),
            str(plan.questions[1].question_id),
        }

    asyncio.run(run_test())


def test_existing_source_is_reused_and_metadata_is_preserved() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            aggregate,
            run,
            plan,
        ) = await setup_searching_run()

        existing = Source(
            source_type=SourceType.WEB,
            title="Existing source",
            url=("https://example.com/shared?utm_campaign=old"),
            metadata={
                "owner": "manual",
            },
        )

        aggregate.add_source(existing)

        await research_repository.save(aggregate)

        first_query = plan.questions[0].question
        second_query = plan.questions[1].question

        result = SearchResult(
            title="Search title",
            url="https://example.com/shared#fragment",
            snippet="snippet",
            provider="fake",
        )
        provider = RecordingProvider({first_query: [result], second_query: [result]})

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            provider,
        )

        touched = await service.search_run(
            aggregate.task.task_id,
            run.run_id,
        )

        stored = await research_repository.get(aggregate.task.task_id)

        assert stored is not None

        assert len(stored.sources) == 1

        assert touched[0].source_id == existing.source_id

        stored_source = stored.sources[existing.source_id]

        assert stored_source.metadata["owner"] == "manual"

        assert len(stored_source.metadata["search"]["hits"]) == 2

    asyncio.run(run_test())


def test_repeated_search_does_not_duplicate_sources_or_hits() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            aggregate,
            run,
            plan,
        ) = await setup_searching_run()

        first_query = plan.questions[0].question
        second_query = plan.questions[1].question

        result = SearchResult(title="One", url="https://example.com/one", snippet="one", provider="fake")
        provider = RecordingProvider({first_query: [result], second_query: [result]})

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            provider,
        )

        await service.search_run(
            aggregate.task.task_id,
            run.run_id,
        )

        await service.search_run(
            aggregate.task.task_id,
            run.run_id,
        )

        stored = await research_repository.get(aggregate.task.task_id)

        assert stored is not None

        assert len(stored.sources) == 1

        source = next(iter(stored.sources.values()))

        assert len(source.metadata["search"]["hits"]) == 2

    asyncio.run(run_test())


def test_missing_task_is_rejected() -> None:
    async def run_test() -> None:
        service = ResearchSearchService(
            InMemoryResearchRepository(),
            InMemoryResearchRunRepository(),
            InMemoryResearchPlanRepository(),
            RecordingProvider({}),
        )

        with pytest.raises(ResearchTaskNotFoundError):
            await service.search_run(
                uuid4(),
                uuid4(),
            )

    asyncio.run(run_test())


def test_wrong_run_state_is_rejected_before_provider_call() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            aggregate,
            run,
            _plan,
        ) = await setup_searching_run()

        run = ResearchRun.create(aggregate.task.task_id)

        run.advance(ResearchRunStatus.PLANNING)

        await run_repository.save(run)

        provider = RecordingProvider({})

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            provider,
        )

        with pytest.raises(
            ResearchSearchError,
            match="searching",
        ):
            await service.search_run(
                aggregate.task.task_id,
                run.run_id,
            )

        assert provider.calls == []

    asyncio.run(run_test())


def test_missing_plan_is_rejected_before_provider_call() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            _plan_repository,
            aggregate,
            run,
            _plan,
        ) = await setup_searching_run()

        empty_plan_repository = InMemoryResearchPlanRepository()

        provider = RecordingProvider({})

        service = ResearchSearchService(
            research_repository,
            run_repository,
            empty_plan_repository,
            provider,
        )

        with pytest.raises(
            ResearchSearchError,
            match="plan not found",
        ):
            await service.search_run(
                aggregate.task.task_id,
                run.run_id,
            )

        assert provider.calls == []

    asyncio.run(run_test())


def test_wrong_task_run_pair_is_rejected() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            _aggregate,
            run,
            _plan,
        ) = await setup_searching_run()

        provider = RecordingProvider({})

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            provider,
        )

        with pytest.raises(ResearchTaskNotFoundError):
            await service.search_run(
                uuid4(),
                run.run_id,
            )

        assert provider.calls == []

    asyncio.run(run_test())


def test_provider_failure_does_not_persist_partial_sources() -> None:
    async def run_test() -> None:
        (
            research_repository,
            run_repository,
            plan_repository,
            aggregate,
            run,
            _plan,
        ) = await setup_searching_run()

        service = ResearchSearchService(
            research_repository,
            run_repository,
            plan_repository,
            FailingProvider(),
        )

        with pytest.raises(
            ResearchSearchError,
            match="Search provider failed",
        ):
            await service.search_run(
                aggregate.task.task_id,
                run.run_id,
            )

        stored = await research_repository.get(aggregate.task.task_id)

        assert stored is not None

        assert stored.sources == {}

    asyncio.run(run_test())


def test_empty_search_results_fail_without_marking_any_source_as_persisted() -> None:
    async def run_test() -> None:
        research, runs, plans, aggregate, run, plan = await setup_searching_run()
        provider = RecordingProvider({})
        service = ResearchSearchService(research, runs, plans, provider)
        with pytest.raises(ResearchSearchError, match="Search produced no sources"):
            await service.search_run(aggregate.task.task_id, run.run_id)
        stored = await research.get(aggregate.task.task_id)
        assert stored is not None
        assert stored.sources == {}
        assert len(provider.calls) == len(plan.questions)

    asyncio.run(run_test())


def test_one_missing_question_fails_without_committing_other_question_sources() -> None:
    async def run_test() -> None:
        research, runs, plans, aggregate, run, plan = await setup_searching_run()
        provider = RecordingProvider(
            {
                plan.questions[0].question: [
                    SearchResult(
                        title="First source",
                        url="https://example.com/partial",
                        snippet="Partial",
                        provider="fake",
                    )
                ],
            }
        )
        service = ResearchSearchService(research, runs, plans, provider)
        with pytest.raises(ResearchSearchError, match=str(plan.questions[1].question_id)):
            await service.search_run(aggregate.task.task_id, run.run_id)
        stored = await research.get(aggregate.task.task_id)
        assert stored is not None
        assert stored.sources == {}

    asyncio.run(run_test())


def test_financial_discovery_enriches_search_and_tracks_only_eligible_candidates() -> None:
    from enterprise_research.application.financial_source_discovery import (
        FinancialSourceDiscoverySettings,
        financial_discovery_query,
    )

    async def run_test() -> None:
        research, runs, plans, aggregate, run, plan = await setup_searching_run()
        policy = FinancialSourceDiscoverySettings(target_year="2024", preferred_domains=("microsoft.com",))
        responses = {
            financial_discovery_query(question.question, policy): [
                SearchResult(
                    title="Microsoft 2026 Annual Report",
                    url="https://www.microsoft.com/investor/reports/ar26/",
                    provider="fixture",
                ),
                SearchResult(
                    title="FY2024 annual report (third-party)",
                    url="https://www.microsoft.com.evil.example/investor/reports/ar24/",
                    provider="fixture",
                ),
                SearchResult(
                    title="Opaque financial source",
                    url="https://cdn.example/opaque-document",
                    provider="fixture",
                ),
                SearchResult(
                    title="Microsoft 2024 Annual Report",
                    url="https://www.microsoft.com/investor/reports/ar24/index.html",
                    provider="fixture",
                ),
            ]
            for question in plan.questions
        }
        provider = RecordingProvider(responses)
        service = ResearchSearchService(research, runs, plans, provider, financial_discovery=policy)
        touched = await service.search_run(aggregate.task.task_id, run.run_id)
        stored = await research.get(aggregate.task.task_id)
        assert stored is not None
        assert len(provider.calls) == len(plan.questions)
        assert all(max_results == 5 for _, max_results in provider.calls)
        assert all(query.startswith("FY2024 original annual report") for query, _ in provider.calls)
        assert len(stored.sources) == 3
        assert "ar26" not in " ".join(str(source.url) for source in touched)
        assert str(touched[0].url) == "https://www.microsoft.com/investor/reports/ar24/index.html"
        first_hit = touched[0].metadata["search"]["hits"][0]
        assert first_hit["source_year_discovery"] == {
            "target_year": "2024",
            "edition_hint": "matching",
            "preferred_domain": True,
        }
        assert first_hit["query"] == provider.calls[0][0]
        opaque = next(source for source in touched if "opaque-document" in str(source.url))
        assert opaque.metadata["search"]["hits"][0]["source_year_discovery"]["edition_hint"] == "unknown"
        assert [question.question for question in plan.questions] == [
            "How reliable is durable execution?",
            "How does recovery work?",
        ]

    asyncio.run(run_test())


def test_financial_discovery_wrong_editions_fail_before_persisting_any_sources() -> None:
    from enterprise_research.application.financial_source_discovery import (
        FinancialSourceDiscoverySettings,
        financial_discovery_query,
    )

    async def run_test() -> None:
        research, runs, plans, aggregate, run, plan = await setup_searching_run()
        policy = FinancialSourceDiscoverySettings(target_year="2024")
        bad_results = [
            SearchResult(
                title="Microsoft 2026 Annual Report",
                url="https://www.microsoft.com/investor/reports/ar26/index.html",
                provider="fixture",
            )
        ]
        provider = RecordingProvider(
            {financial_discovery_query(question.question, policy): bad_results for question in plan.questions}
        )
        service = ResearchSearchService(research, runs, plans, provider, financial_discovery=policy)
        with pytest.raises(ResearchSearchError, match="Search produced no sources"):
            await service.search_run(aggregate.task.task_id, run.run_id)
        stored = await research.get(aggregate.task.task_id)
        assert stored is not None
        assert stored.sources == {}
        assert len(provider.calls) == len(plan.questions)

    asyncio.run(run_test())


def test_financial_discovery_deduplicates_repeated_searches() -> None:
    from enterprise_research.application.financial_source_discovery import (
        FinancialSourceDiscoverySettings,
        financial_discovery_query,
    )

    async def run_test() -> None:
        research, runs, plans, aggregate, run, plan = await setup_searching_run()
        policy = FinancialSourceDiscoverySettings(target_year="2024")
        results = [
            SearchResult(
                title="Microsoft 2024 Annual Report",
                url="https://www.microsoft.com/investor/reports/ar24/index.html",
                provider="fixture",
            )
        ]
        provider = RecordingProvider({financial_discovery_query(q.question, policy): results for q in plan.questions})
        service = ResearchSearchService(research, runs, plans, provider, financial_discovery=policy)
        await service.search_run(aggregate.task.task_id, run.run_id)
        await service.search_run(aggregate.task.task_id, run.run_id)
        stored = await research.get(aggregate.task.task_id)
        assert stored is not None
        assert len(stored.sources) == 1
        source = next(iter(stored.sources.values()))
        assert len(source.metadata["search"]["hits"]) == len(plan.questions)

    asyncio.run(run_test())
