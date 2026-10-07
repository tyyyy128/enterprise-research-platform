from enterprise_research.application.research_service import (
    ResearchService,
)

from enterprise_research.infrastructure.memory_task_repository import (
    MemoryResearchTaskRepository,
)


def test_create_task_persisted():

    repository = MemoryResearchTaskRepository()

    service = ResearchService(
        repository
    )

    task = service.create_task(
        "AI Market Research",
        "Analyze enterprise AI trends",
    )

    loaded = repository.get(
        task.id
    )

    assert loaded == task


def test_full_research_workflow_creation():

    repository = MemoryResearchTaskRepository()

    service = ResearchService(
        repository
    )

    task = service.create_task(
        "Enterprise AI Analysis",
        "Research AI adoption trends",
    )


    question = service.add_question(
        task,
        "What are the key enterprise AI trends?",
    )


    source = service.collect_source(
        "https://example.com/report",
        "Enterprise AI Report",
    )


    evidence = service.extract_evidence(
        source,
        "AI adoption is increasing rapidly.",
    )


    claim = service.create_claim(
        "Enterprise AI adoption will continue growing.",
    )


    report = service.create_report(
        task,
        "Enterprise AI Research Report",
    )


    assert question.task_id == task.id

    assert evidence.source_id == source.id

    assert claim.statement == (
        "Enterprise AI adoption will continue growing."
    )

    assert report.task_id == task.id
