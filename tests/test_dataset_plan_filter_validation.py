"""Validate filter semantics through the real save/validate tool boundary."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from db_rag.catalog import SchemaCatalog
from db_rag.catalog_relationships import CatalogRelationshipSpec
from db_rag.study import DuckDbStudyDataSource
from epi_agent.artifacts import StateArtifactStore
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_call_protocol import tool_error_content


@pytest.fixture
def runtime(tmp_path: Path):
    path = tmp_path / "study.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE measurements (amount DOUBLE, count_value INTEGER, label VARCHAR, measured_on DATE)")
        connection.execute("INSERT INTO measurements VALUES (10, 10, 'alpha', '2020-01-01'), (20, 20, 'beta', '2020-01-02')")
    source = "example-study"
    catalog = SchemaCatalog({"tables": [{"table": "measurements"}],
        "columns": [{"table": "measurements", "column": name}
            for name in ("amount", "count_value", "label", "measured_on")]}, default_source_id=source)
    study = StudyBundle(study_id=source, label=source, knowledge=None, catalog=catalog,
        data_sources={source: DuckDbStudyDataSource(path, CatalogRelationshipSpec(table_keys={}))}, source_id=source)
    context = ToolContext(studies=StudyRegistry([study]), artifact_store=StateArtifactStore(),
        thread_id="filter-validation", policy=object())
    return context, path


def constraint(operator, value, *, column="amount"):
    return {"source": "example-study", "table": "measurements", "column": column,
        "operator": operator, **({"values": value} if isinstance(value, list) else {"value": value})}


def validate(context, *constraints):
    registry = build_db_rag_tool_registry()
    result = registry.invoke("dbrag-save_dataset_plan", {"plan": {
        "study_id": "example-study", "goal": "Select measurements using the requested filters.",
        "concepts": [{"concept_id": "measurement", "label": "Measurement",
            "fields": [{"source": "example-study", "table": "measurements", "column": "amount"}]}],
        "filters": [{"description": "Requested condition", "value_constraints": [c]} for c in constraints],
    }}, context=context)
    reference = result.artifacts[0]
    return registry.invoke("dbrag-validate_dataset_plan", {
        "plan_id": reference.id, "plan_version": reference.version}, context=context)


@pytest.mark.parametrize(("operator", "value", "expected_rows"), [
    (">", 0, 2), (">=", 0, 2), ("<", 15, 1), ("<=", 15, 1),
    ("!=", 0, 2), ("<>", 0, 2), (">", 100, 0), ("<", 0, 0),
])
def test_absent_threshold_or_exclusion_is_valid(runtime, operator, value, expected_rows):
    context, path = runtime
    validate(context, constraint(operator, value))
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(f"SELECT count(*) FROM measurements WHERE amount {operator} ?", [value]).fetchone()[0] == expected_rows


@pytest.mark.parametrize("operator", ["not in", "NOT IN"])
def test_excluding_absent_values_is_valid(runtime, operator):
    context, path = runtime
    validate(context, constraint(operator, [0, 15]))
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM measurements WHERE amount NOT IN (0, 15)").fetchone()[0] == 2


def test_nonnull_check_is_valid_when_no_stored_null_exists(runtime):
    context, _ = runtime
    validate(context, constraint("!=", None))


@pytest.mark.parametrize("pattern", ["a%", "no-match%"])
def test_pattern_need_not_be_a_stored_literal(runtime, pattern):
    context, _ = runtime
    validate(context, constraint("ilike", pattern, column="label"))


@pytest.mark.parametrize(("operator", "value", "accepted"), [
    ("=", 10, True), ("==", 10, True), ("=", 0, False), ("==", 0, False),
    ("in", [10, 20], True), ("IN", [10, 20], True), ("in", [10, 0], False),
])
def test_positive_equality_and_membership_keep_observed_value_rule(runtime, operator, value, accepted):
    context, _ = runtime
    if accepted:
        validate(context, constraint(operator, value))
    else:
        with pytest.raises(ToolExecutionError) as captured:
            validate(context, constraint(operator, value))
        assert captured.value.code == "PLAN_FILTER_VALUE_UNAVAILABLE"
        assert any(issue["code"] == "PLAN_FILTER_VALUE_UNAVAILABLE" for issue in captured.value.details["issues"])


@pytest.mark.parametrize("empty", [False, True])
def test_incompatible_comparison_value_still_fails(runtime, empty):
    context, path = runtime
    if empty:
        with duckdb.connect(str(path)) as connection:
            connection.execute("DELETE FROM measurements")
    with pytest.raises(ToolExecutionError) as captured:
        validate(context, constraint(">", "not-a-number"))
    assert captured.value.code == "PLAN_FILTER_VALUE_UNAVAILABLE"


def test_comparison_can_validate_against_empty_table(runtime):
    context, path = runtime
    with duckdb.connect(str(path)) as connection:
        connection.execute("DELETE FROM measurements")
    validate(context, constraint(">", 0))


@pytest.mark.parametrize("value", [10.5, 1e30])
def test_numeric_threshold_is_not_narrowed_to_integer_column(runtime, value):
    context, _ = runtime
    validate(context, constraint(">", value, column="count_value"))


def test_independent_errors_are_preserved_in_model_visible_feedback(runtime):
    context, _ = runtime
    with pytest.raises(ToolExecutionError) as captured:
        validate(context, constraint(">", "not-a-number"), constraint("=", 0),
            constraint(">", "not-a-date", column="measured_on"))
    payload = json.loads(tool_error_content(captured.value))["error"]
    issues = payload["details"]["issues"]
    assert all(any(issue.get("path", "").startswith(f"filters[{i}]") for issue in issues) for i in range(3))
    assert payload["recoverable"] is True
