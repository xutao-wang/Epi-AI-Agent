from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from db_rag.service.models import ValidatedExtractionSql
from epi_agent.artifacts import DatasetPlan, StateArtifactStore
from epi_agent.db_rag.persistence import persist_sql_subset_artifact
from epi_agent.db_rag.quality import inspect_dataset
from epi_agent.db_rag.tools import _approved_plan, _validated_sql_artifact
from epi_agent.protocol import ArtifactRef, ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from graph.state import MetaKeys
from utils.user_storage import UserStorageLayout


def _plan(study_id: str) -> DatasetPlan:
    return DatasetPlan(
        study_id=study_id,
        goal="List ages",
        dataset_title="Ages",
        row_definition="One row per person.",
        concepts=[],
        required_fields=[],
        operations=[],
    )


def _context(store: StateArtifactStore) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(
            [
                StudyBundle(
                    study_id="study-one",
                    label="Study One",
                    knowledge=None,
                    catalog=None,
                    data_sources={},
                ),
                StudyBundle(
                    study_id="study-two",
                    label="Study Two",
                    knowledge=None,
                    catalog=None,
                    data_sources={},
                ),
            ]
        ),
        artifact_store=store,
        thread_id="thread-1",
        policy=object(),
    )


def _candidate_and_execution() -> tuple[SimpleNamespace, SimpleNamespace]:
    candidate = SimpleNamespace(
        sql='SELECT "age" FROM cohort',
        tables=["cohort"],
        columns=[
            {
                "table": "cohort",
                "column": "age",
                "output_column": "age",
                "purpose": "analysis",
            }
        ],
        source_question="List ages",
        goal_text="List ages",
    )
    execution = SimpleNamespace(
        dataframe=pd.DataFrame({"age": [42]}),
        sql=candidate.sql,
        source_tables=["cohort"],
    )
    return candidate, execution


def test_validated_sql_inherits_the_approved_plan_study() -> None:
    store = StateArtifactStore()
    context = _context(store)
    plan = _plan("study-two")

    stored = _validated_sql_artifact(
        context,
        plan_id="plan-1",
        plan_version=1,
        plan=plan,
        validated=ValidatedExtractionSql(
            sql='SELECT "age" FROM cohort',
            sha256="sha",
        ),
        origin="test",
        tables=["cohort"],
        columns=[],
    )

    assert stored.provenance["study_id"] == "study-two"


def test_approved_plan_reports_when_its_study_is_not_installed() -> None:
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(_plan("missing-study"), status="approved")
    context = _context(store)

    with pytest.raises(ToolExecutionError) as raised:
        _approved_plan(
            context,
            plan_id=plan_ref.id,
            plan_version=plan_ref.version,
        )

    assert raised.value.code == "PLAN_STUDY_UNAVAILABLE"


def test_reused_validated_sql_cannot_cross_studies() -> None:
    store = StateArtifactStore()
    context = _context(store)
    store.save_artifact(
        kind="validated_sql",
        status="approved",
        content={
            "status": "validated",
            "sql": 'SELECT "age" FROM cohort',
            "sql_sha256": "sha",
            "plan_id": "plan-1",
            "plan_version": 1,
        },
        provenance={"study_id": "study-one"},
    )

    with pytest.raises(ToolExecutionError) as raised:
        _validated_sql_artifact(
            context,
            plan_id="plan-1",
            plan_version=1,
            plan=_plan("study-two"),
            validated=ValidatedExtractionSql(
                sql='SELECT "age" FROM cohort',
                sha256="sha",
            ),
            origin="test",
            tables=["cohort"],
            columns=[],
        )

    assert raised.value.code == "STUDY_REFERENCE_MISMATCH"


def test_sql_subset_persistence_records_study_lineage(tmp_path) -> None:
    scope = UserStorageLayout(tmp_path).thread("user-1", "thread-1")
    candidate, execution = _candidate_and_execution()

    _state, artifact, _staged = persist_sql_subset_artifact(
        {"meta": {MetaKeys.THREAD_ID: scope.thread_id}, "artifacts": {}},
        candidate,
        {"tables": ["cohort"], "columns": candidate.columns},
        execution,
        study_id="study-two",
        selection_artifact_id="plan-1",
        sql_candidate_artifact_id="sql-1",
        plan_id="plan-1",
        plan_version=1,
        sql_version=1,
        dataset_id="subset-1",
        runtime_root=scope,
    )

    assert artifact["provenance"]["study_id"] == "study-two"


def test_persistence_attempt_identity_includes_study_id() -> None:
    store = StateArtifactStore()
    lineage = {
        "study_id": "study-two",
        "approved_selected_columns": [],
        "approved_selected_tables": [],
        "expected_output_aliases": [],
        "plan_content_sha256": "a" * 64,
        "thread_id": "thread-1",
        "plan_id": "plan-1",
        "plan_version": 1,
        "sql_content_sha256": "b" * 64,
        "sql_id": "sql-1",
        "sql_version": 1,
        "predecessor_dataset_id": None,
        "predecessor_dataset_version": None,
    }

    attempt = store.begin_dataset_persistence_attempt(
        {
            "dataset_id": "dataset-1",
            "state": "begun",
            "lineage": lineage,
            "expected_final_paths": {
                "path": "/tmp/dataset.parquet",
                "schema_path": "/tmp/dataset.schema.json",
                "metadata_path": "/tmp/dataset.metadata.json",
            },
            "expected_staging_paths": {
                "path": "/tmp/staged.parquet",
                "schema_path": "/tmp/staged.schema.json",
                "metadata_path": "/tmp/staged.metadata.json",
            },
        }
    )

    assert attempt["lineage"]["study_id"] == "study-two"


def test_dataset_inspection_rejects_cross_study_plan_lineage() -> None:
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(_plan("study-one"), status="approved")
    dataset_ref = store.save_dataset(
        {
            "id": "dataset-1",
            "kind": "subset",
            "version": 1,
            "status": "pending_review",
            "provenance": {
                "study_id": "study-two",
                "plan_id": plan_ref.id,
                "plan_version": plan_ref.version,
            },
        },
        make_active=False,
    )

    with pytest.raises(ValueError, match="STUDY_REFERENCE_MISMATCH"):
        inspect_dataset(
            artifact_store=store,
            dataset_ref=dataset_ref,
            plan_ref=ArtifactRef(
                id=plan_ref.id,
                kind="dataset_plan",
                version=plan_ref.version,
            ),
        )


def test_replacement_dataset_cannot_cross_studies() -> None:
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(_plan("study-one"), status="approved")
    predecessor_ref = store.save_dataset(
        {
            "id": "dataset-1",
            "kind": "subset",
            "version": 1,
            "status": "pending_review",
            "provenance": {
                "study_id": "study-two",
                "plan_id": plan_ref.id,
                "plan_version": plan_ref.version,
            },
        },
        make_active=False,
    )

    with pytest.raises(ValueError, match="STUDY_REFERENCE_MISMATCH"):
        store.save_replacement_dataset(
            {
                "id": "dataset-2",
                "kind": "subset",
                "version": 1,
                "status": "pending_review",
                "provenance": {
                    "study_id": "study-one",
                    "plan_id": plan_ref.id,
                    "plan_version": plan_ref.version,
                },
            },
            predecessor_ref=predecessor_ref,
            plan_ref=plan_ref,
            feedback_ref=ArtifactRef(
                id="unused",
                kind="dataset_review_feedback",
                version=1,
            ),
        )


def test_quality_report_inherits_dataset_and_plan_study(tmp_path) -> None:
    scope = UserStorageLayout(tmp_path).thread("user-1", "thread-1")
    store = StateArtifactStore()
    plan_ref = store.save_dataset_plan(_plan("study-two"), status="approved")
    candidate, execution = _candidate_and_execution()
    _state, artifact, _staged = persist_sql_subset_artifact(
        {"meta": {MetaKeys.THREAD_ID: scope.thread_id}, "artifacts": {}},
        candidate,
        {"tables": ["cohort"], "columns": candidate.columns},
        execution,
        study_id="study-two",
        selection_artifact_id=plan_ref.id,
        sql_candidate_artifact_id="sql-1",
        plan_id=plan_ref.id,
        plan_version=plan_ref.version,
        sql_version=1,
        dataset_id="subset-1",
        runtime_root=scope,
    )
    dataset_ref = store.save_dataset(artifact, make_active=False)

    report_ref = inspect_dataset(
        artifact_store=store,
        dataset_ref=dataset_ref,
        plan_ref=ArtifactRef(
            id=plan_ref.id,
            kind="dataset_plan",
            version=plan_ref.version,
        ),
    )
    report = store.require(report_ref)

    assert report.provenance["study_id"] == "study-two"
