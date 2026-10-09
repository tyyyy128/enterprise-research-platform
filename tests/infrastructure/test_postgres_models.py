from enterprise_research.infrastructure.postgres import Base


def test_expected_postgres_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "research_tasks",
        "research_sources",
        "research_source_documents",
        "research_evidence",
        "research_claims",
        "claim_evidence",
        "research_reports",
        "research_runs",
        "research_run_stage_attempts",
        "research_plan_questions",
        "research_plans",
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


def test_research_run_references_task() -> None:
    table = Base.metadata.tables["research_runs"]

    foreign_keys = {foreign_key.target_fullname for foreign_key in table.foreign_keys}

    assert foreign_keys == {"research_tasks.id"}
    assert table.columns["task_id"].nullable is False
    assert table.columns["status"].nullable is False
    assert table.columns["error"].nullable is True


def test_research_plan_references_run() -> None:
    table = Base.metadata.tables["research_plans"]

    foreign_keys = {foreign_key.target_fullname for foreign_key in table.foreign_keys}

    assert foreign_keys == {"research_runs.id"}
    assert table.columns["run_id"].nullable is False
    assert table.columns["run_id"].unique is True


def test_research_plan_question_references_plan() -> None:
    table = Base.metadata.tables["research_plan_questions"]

    foreign_keys = {foreign_key.target_fullname for foreign_key in table.foreign_keys}

    assert foreign_keys == {"research_plans.id"}
    assert table.columns["plan_id"].nullable is False
    assert table.columns["position"].nullable is False


def test_retrieved_document_references_source() -> None:
    table = Base.metadata.tables["research_source_documents"]

    foreign_keys = {foreign_key.target_fullname for foreign_key in table.foreign_keys}

    assert foreign_keys == {"research_sources.id"}

    assert {column.name for column in table.primary_key.columns} == {"source_id"}

    assert "metadata" in table.columns


def test_research_stage_attempts_reference_run_and_are_uniquely_fenced() -> None:
    table = Base.metadata.tables["research_run_stage_attempts"]
    assert {fk.target_fullname for fk in table.foreign_keys} == {"research_runs.id"}
    assert {column.name for column in table.primary_key.columns} == {"run_id", "stage", "fencing_token"}
    assert table.columns["finished_at"].nullable is True
    assert table.columns["started_at"].nullable is False
    assert table.columns["outcome"].nullable is False
    assert {constraint.name for constraint in table.constraints if constraint.name} >= {
        "ck_research_run_stage_attempts_finished_shape",
        "ck_research_run_stage_attempts_fence",
        "ck_research_run_stage_attempts_time_order",
    }
