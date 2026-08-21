from __future__ import annotations

import json

import pytest

from db_rag.catalog import SchemaEvidenceHit
from epi_agent.artifacts import (
    DatasetPlan,
    DatasetPlanConcept,
    PlanField,
    PlanOperation,
    StateArtifactStore,
)
from epi_agent.db_rag.prompt import DB_RAG_SYSTEM_PROMPT
from epi_agent.db_rag.reviews import _plan_review_view
from epi_agent.db_rag.tools import (
    _profile_matches_operation,
    _safe_relationship_profile,
    build_db_rag_tool_registry,
)
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry


class _Catalog:
    def __init__(self, study_id: str, table: str, column: str) -> None:
        self.study_id = study_id
        self.table = table
        self.column = column
        self.searches: list[tuple[list[str], int]] = []
        self.inspections: list[tuple[str, str, int, int]] = []

    def search_many(self, queries: list[str], *, limit: int):
        self.searches.append((queries, limit))
        return [
            [
                SchemaEvidenceHit(
                    source=self.study_id,
                    table=self.table,
                    column=self.column,
                    text=f"{self.column} annotation",
                    provenance={
                        "authority": "runtime_schema_catalog",
                        "source_id": self.study_id,
                        "table": self.table,
                        "column": self.column,
                    },
                    matched_by=("vector", "lexical"),
                )
            ]
            for _query in queries
        ]

    def inspect_table(
        self,
        source: str,
        table: str,
        *,
        offset: int = 0,
        limit: int = 25,
    ):
        self.inspections.append((source, table, offset, limit))
        return [
            SchemaEvidenceHit(
                source=source,
                table=table,
                column=self.column,
                text=f"Exact {self.column}",
                provenance={
                    "authority": "runtime_schema_catalog",
                    "source_id": source,
                    "table": table,
                    "column": self.column,
                },
            )
        ]

    def field_exists(self, table: str, column: str) -> bool:
        return table == self.table and column == self.column


class _RelationshipInventory:
    def find_join_paths(
        self,
        left_table: str,
        right_table: str,
        *,
        max_hops: int,
        max_paths: int,
    ):
        return [
            {
                "tables": [left_table, right_table],
                "profiles": [
                    self.profile_relationship(
                        left_table,
                        right_table,
                        [("CHILD_TOKEN", "ADULT_TOKEN")],
                    )
                ],
            }
        ][:max_paths]

    def profile_relationship(
        self,
        left_table: str,
        right_table: str,
        key_pairs: list[tuple[str, str]],
    ):
        return {
            "left_table": left_table,
            "right_table": right_table,
            "key_pairs": key_pairs,
            "left_distinct_keys": 2,
            "right_distinct_keys": 1,
            "matched_keys": 1,
            "joined_rows": 2,
            "left_cardinality": "many",
            "right_cardinality": "one",
            "warnings": [],
            "relationship_evidence": [
                {
                    "left_column": key_pairs[0][0],
                    "right_column": key_pairs[0][1],
                    "left_join_key": "child_key",
                    "right_join_key": "adult_key",
                    "source": "declared_relationship",
                    "relationship_id": "guardian_link",
                    "expected_cardinality": "many_to_one",
                    "note": "Each child references one guardian.",
                    "direction": "forward",
                }
            ],
        }


class _RelationshipSource:
    def relationship_inventory(self) -> _RelationshipInventory:
        return _RelationshipInventory()


def _study(study_id: str, catalog: _Catalog) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=study_id,
        knowledge=None,
        catalog=catalog,
        data_sources={study_id: object()},
        source_id=study_id,
    )


def _context(*studies: StudyBundle) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(studies),
        artifact_store=StateArtifactStore(),
        thread_id="thread-1",
        policy=object(),
    )


def _review_plan(study_id: str = "study-first") -> DatasetPlan:
    return DatasetPlan(
        study_id=study_id,
        goal="Extract the requested field.",
        row_definition="One row per participant.",
        concepts=[
            DatasetPlanConcept(
                concept_id="requested_field",
                label="Requested field",
                retrieval_probe="requested field",
                fields=[
                    {
                        "source": "study-first",
                        "table": "FIRST_TABLE",
                        "column": "FIRST_FIELD",
                        "purpose": "analysis",
                        "roles": ["requested"],
                    }
                ],
            )
        ],
        required_fields=[
            PlanField(
                source="study-first",
                table="FIRST_TABLE",
                column="FIRST_FIELD",
                purpose="identity",
                roles={"identifier"},
            )
        ],
        operations=[
            PlanOperation(name="select", description="Select approved fields.")
        ],
    )


def test_plan_review_resolves_join_paths_from_plan_study_in_multi_study_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _study(
        "study-first",
        _Catalog("study-first", "FIRST_TABLE", "FIRST_FIELD"),
    )
    second = _study(
        "study-second",
        _Catalog("study-second", "SECOND_TABLE", "SECOND_FIELD"),
    )
    observed: list[str] = []

    def verified_join_paths(plan: DatasetPlan, study: StudyBundle):
        observed.append(study.study_id)
        assert plan.study_id == "study-first"
        assert set(study.data_sources) == {"study-first"}
        return []

    monkeypatch.setattr(
        "epi_agent.db_rag.tools._verified_join_paths",
        verified_join_paths,
    )

    view = _plan_review_view(
        _review_plan(),
        context=_context(first, second),
        plan_id="plan-1",
        version=1,
    )

    assert observed == ["study-first"]
    assert view["joins"] == []


def test_plan_review_rejects_unavailable_plan_study() -> None:
    context = _context(
        _study(
            "study-first",
            _Catalog("study-first", "FIRST_TABLE", "FIRST_FIELD"),
        ),
        _study(
            "study-second",
            _Catalog("study-second", "SECOND_TABLE", "SECOND_FIELD"),
        ),
    )

    with pytest.raises(ToolExecutionError) as raised:
        _plan_review_view(
            _review_plan("missing-study"),
            context=context,
            plan_id="plan-1",
            version=1,
        )

    assert raised.value.code == "STUDY_NOT_AVAILABLE"
    assert raised.value.recoverable is True


def test_catalog_search_scopes_each_call_and_emits_structured_refs() -> None:
    first = _Catalog("study-first", "FIRST_TABLE", "FIRST_FIELD")
    second = _Catalog("study-second", "SECOND_TABLE", "SECOND_FIELD")
    context = _context(
        _study("study-first", first),
        _study("study-second", second),
    )
    registry = build_db_rag_tool_registry()

    second_result = registry.invoke(
        "dbrag-search_catalog",
        {
            "study_id": "study-second",
            "queries": ["second concept"],
            "limit": 5,
        },
        context=context,
    )
    first_result = registry.invoke(
        "dbrag-search_catalog",
        {
            "study_id": "study-first",
            "queries": ["first concept"],
            "limit": 5,
        },
        context=context,
    )

    assert first.searches == [(["first concept"], 5)]
    assert second.searches == [(["second concept"], 5)]
    second_message = json.loads(second_result.message)
    second_hit = second_message["probes"][0]["hits"][0]
    assert second_message["study_id"] == "study-second"
    assert second_hit["field_ref"] == {
        "study_id": "study-second",
        "source_id": "study-second",
        "table": "SECOND_TABLE",
        "column": "SECOND_FIELD",
    }
    second_artifact = context.artifact_store.require(second_result.artifacts[0])
    assert second_artifact.provenance["study_id"] == "study-second"
    assert context.artifact_store.require(first_result.artifacts[0]).provenance[
        "study_id"
    ] == "study-first"


def test_inspect_table_consumes_the_search_table_ref() -> None:
    catalog = _Catalog("study-one", "TABLE_ONE", "FIELD_ONE")
    context = _context(_study("study-one", catalog))
    registry = build_db_rag_tool_registry()
    search = registry.invoke(
        "dbrag-search_catalog",
        {"study_id": "study-one", "queries": ["concept"], "limit": 5},
        context=context,
    )
    field_ref = json.loads(search.message)["probes"][0]["hits"][0]["field_ref"]
    table_ref = {
        key: field_ref[key]
        for key in ("study_id", "source_id", "table")
    }

    inspected = registry.invoke(
        "dbrag-inspect_table",
        {"table_ref": table_ref, "offset": 0, "limit": 25},
        context=context,
    )

    message = json.loads(inspected.message)
    assert catalog.inspections == [("study-one", "TABLE_ONE", 0, 26)]
    assert message["table_ref"] == table_ref
    assert message["fields"][0]["field_ref"] == {
        **table_ref,
        "column": "FIELD_ONE",
    }


def test_unknown_catalog_study_never_touches_another_catalog() -> None:
    catalog = _Catalog("only-study", "TABLE_ONE", "FIELD_ONE")
    context = _context(_study("only-study", catalog))

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-search_catalog",
            {"study_id": "missing", "queries": ["concept"], "limit": 5},
            context=context,
        )

    assert raised.value.code == "STUDY_NOT_AVAILABLE"
    assert catalog.searches == []


def test_relationship_discovery_rejects_cross_study_refs_before_access() -> None:
    first = _Catalog("study-first", "FIRST_TABLE", "FIRST_FIELD")
    second = _Catalog("study-second", "SECOND_TABLE", "SECOND_FIELD")
    context = _context(
        _study("study-first", first),
        _study("study-second", second),
    )

    with pytest.raises(ToolExecutionError) as raised:
        build_db_rag_tool_registry().invoke(
            "dbrag-find_join_paths",
            {
                "required_fields": [
                    {
                        "study_id": "study-first",
                        "source_id": "study-first",
                        "table": "FIRST_TABLE",
                        "column": "FIRST_FIELD",
                    },
                    {
                        "study_id": "study-second",
                        "source_id": "study-second",
                        "table": "SECOND_TABLE",
                        "column": "SECOND_FIELD",
                    },
                ],
                "max_hops": 3,
                "max_paths": 10,
            },
            context=context,
        )

    assert raised.value.code == "CROSS_STUDY_OPERATION_UNAVAILABLE"


def test_db_rag_prompt_requires_scalar_study_scope_and_exact_refs() -> None:
    prompt = " ".join(DB_RAG_SYSTEM_PROMPT.split())
    assert "one exact scalar study_id" in prompt
    assert "copy the returned table_ref and field_ref" in prompt
    assert "separate catalog calls" in prompt
    assert "Never combine studies in one dataset plan" in prompt
    assert (
        "Use the exact join columns, direction, and declared relationship "
        "evidence returned by relationship tools. Never rename, substitute, "
        "or infer a join key."
    ) in prompt


def test_db_rag_prompt_requires_evidence_first_clarification_order() -> None:
    prompt = " ".join(DB_RAG_SYSTEM_PROMPT.split())

    for required in (
        "Before asking about database uncertainty",
        "search the runtime catalog",
        "inspect plausible tables",
        "check relationship paths",
        "the user could reasonably provide the missing information",
        "meaning of a user-provided column",
        "report the demonstrated technical limitation",
    ):
        assert required in prompt
    assert "technical failure rather than requesting a clarification" not in prompt


def test_relationship_profile_preserves_both_study_scoped_table_refs() -> None:
    catalog = _Catalog("study-one", "TABLE_ONE", "FIELD_ONE")
    study = StudyBundle(
        study_id="study-one",
        label="study-one",
        knowledge=None,
        catalog=catalog,
        data_sources={"study-one": _RelationshipSource()},
        source_id="study-one",
    )
    context = _context(study)

    result = build_db_rag_tool_registry().invoke(
        "dbrag-profile_relationship",
        {
            "left_table_ref": {
                "study_id": "study-one",
                "source_id": "study-one",
                "table": "LEFT_TABLE",
            },
            "right_table_ref": {
                "study_id": "study-one",
                "source_id": "study-one",
                "table": "RIGHT_TABLE",
            },
            "key_pairs": [
                {"left_column": "SEQN", "right_column": "SEQN"}
            ],
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["left_table_ref"]["table"] == "LEFT_TABLE"
    assert message["right_table_ref"]["table"] == "RIGHT_TABLE"
    artifact = context.artifact_store.require(result.artifacts[0])
    assert artifact.provenance["study_id"] == "study-one"
    evidence = message["profile"]["relationship_evidence"][0]
    assert evidence == {
        "left_column": "SEQN",
        "right_column": "SEQN",
        "left_join_key": "child_key",
        "right_join_key": "adult_key",
        "source": "declared_relationship",
        "relationship_id": "guardian_link",
        "expected_cardinality": "many_to_one",
        "note": "Each child references one guardian.",
        "direction": "forward",
    }
    assert artifact.content["profile"]["relationship_evidence"] == [evidence]


def test_join_path_result_preserves_required_field_refs() -> None:
    class _JoinCatalog(_Catalog):
        def field_exists(self, table: str, column: str) -> bool:
            return column == "SEQN" and table in {"LEFT_TABLE", "RIGHT_TABLE"}

    catalog = _JoinCatalog("study-one", "LEFT_TABLE", "SEQN")
    study = StudyBundle(
        study_id="study-one",
        label="study-one",
        knowledge=None,
        catalog=catalog,
        data_sources={"study-one": _RelationshipSource()},
        source_id="study-one",
    )
    context = _context(study)
    required_fields = [
        {
            "study_id": "study-one",
            "source_id": "study-one",
            "table": table,
            "column": "SEQN",
        }
        for table in ("LEFT_TABLE", "RIGHT_TABLE")
    ]

    result = build_db_rag_tool_registry().invoke(
        "dbrag-find_join_paths",
        {
            "required_fields": required_fields,
            "max_hops": 3,
            "max_paths": 10,
        },
        context=context,
    )

    message = json.loads(result.message)
    assert message["required_fields"] == required_fields
    assert (
        message["paths"][0]["profiles"][0]["relationship_evidence"][0]
        ["relationship_id"]
        == "guardian_link"
    )


def test_relationship_evidence_preserves_exact_physical_edge_matching() -> None:
    profile = _safe_relationship_profile(
        {
            "left_table": "CHILDREN",
            "right_table": "ADULTS",
            "key_pairs": [["CHILD_TOKEN", "ADULT_TOKEN"]],
            "left_distinct_keys": 2,
            "right_distinct_keys": 1,
            "matched_keys": 1,
            "joined_rows": 2,
            "left_cardinality": "many",
            "right_cardinality": "one",
            "warnings": [],
            "relationship_evidence": [
                {
                    "left_column": "CHILD_TOKEN",
                    "right_column": "ADULT_TOKEN",
                    "left_join_key": "child_key",
                    "right_join_key": "adult_key",
                    "source": "declared_relationship",
                    "relationship_id": "guardian_link",
                    "expected_cardinality": "many_to_one",
                    "note": "Each child references one guardian.",
                    "direction": "forward",
                }
            ],
        }
    )

    assert profile["relationship_evidence"][0]["relationship_id"] == "guardian_link"
    assert _profile_matches_operation(
        profile,
        left_table="CHILDREN",
        right_table="ADULTS",
        key_pairs=[("CHILD_TOKEN", "ADULT_TOKEN")],
    )
