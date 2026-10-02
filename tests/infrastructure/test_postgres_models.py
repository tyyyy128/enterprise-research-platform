from enterprise_research.infrastructure.postgres import Base


def test_expected_postgres_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "research_tasks",
        "research_sources",
        "research_evidence",
        "research_claims",
        "claim_evidence",
        "research_reports",
        "report_claims",
    }


def test_research_source_metadata_column_exists() -> None:
    table = Base.metadata.tables["research_sources"]

    assert "metadata" in table.columns
    assert "task_id" in table.columns


def test_evidence_references_task_and_source() -> None:
    table = Base.metadata.tables["research_evidence"]

    foreign_keys = {foreign_key.target_fullname for foreign_key in table.foreign_keys}

    assert "research_tasks.id" in foreign_keys
    assert "research_sources.id" in foreign_keys


def test_claim_evidence_has_composite_primary_key() -> None:
    table = Base.metadata.tables["claim_evidence"]

    assert {column.name for column in table.primary_key.columns} == {
        "claim_id",
        "evidence_id",
    }


def test_report_is_unique_per_task() -> None:
    table = Base.metadata.tables["research_reports"]

    unique_constraints = {
        constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert "uq_research_reports_task_id" in unique_constraints
