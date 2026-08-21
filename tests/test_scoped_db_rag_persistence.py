from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from db_rag.service.models import SqlExecutionResult, ValidatedExtractionSql
from epi_agent.artifacts import DatasetPlan, PlanField, StateArtifactStore
from epi_agent.db_rag.persistence import persist_sql_subset_artifact
from epi_agent.db_rag import tools as db_rag_tools
from epi_agent.protocol import ToolContext
from epi_agent.studies import StudyBundle, StudyRegistry
from graph.state import MetaKeys
from utils.dataset_artifacts import load_dataset_artifact
from utils.user_storage import UserStorageLayout


def test_sql_subset_persistence_uses_authorized_thread_dataset_root(tmp_path) -> None:
    scope = UserStorageLayout(tmp_path).thread("external-user-a", "thread-1")
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
        sql='SELECT "age" FROM cohort',
        source_tables=["cohort"],
    )

    _state, artifact, _staged = persist_sql_subset_artifact(
        {"meta": {MetaKeys.THREAD_ID: scope.thread_id}, "artifacts": {}},
        candidate,
        {"tables": ["cohort"], "columns": candidate.columns},
        execution,
        study_id="cohort-study",
        selection_artifact_id="plan-1",
        sql_candidate_artifact_id="sql-1",
        dataset_id="subset-1",
        runtime_root=scope,
    )

    assert artifact["path"] == str(scope.datasets / "subset-1.parquet")
    dataframe, _schema = load_dataset_artifact(artifact, runtime_root=scope)
    assert dataframe.to_dict(orient="records") == [{"age": 42}]


def test_extraction_wrapper_forwards_authorized_scope_to_sql_persistence(
    tmp_path,
    monkeypatch,
) -> None:
    scope = UserStorageLayout(tmp_path).thread("external-user-a", "thread-1")
    artifact_store = StateArtifactStore.from_state({"artifacts": {}})
    context = ToolContext(
        studies=StudyRegistry(
            [
                StudyBundle(
                    study_id="cohort-study",
                    label="Cohort Study",
                    knowledge=None,
                    catalog=None,
                    data_sources={"cohort_source": object()},
                )
            ]
        ),
        artifact_store=artifact_store,
        thread_id=scope.thread_id,
        thread_storage=scope,
        policy=object(),
    )
    plan = DatasetPlan(
        study_id="cohort-study",
        goal="List ages",
        dataset_title="Cohort ages",
        row_definition="One row per cohort record.",
        concepts=[],
        required_fields=[
            PlanField(
                source="cohort_source",
                table="cohort",
                column="age",
                purpose="analysis",
            )
        ],
        operations=[],
    )
    stored_plan_ref = artifact_store.save_dataset_plan(plan)
    artifact_store.transition_artifact_status(
        stored_plan_ref,
        expected_status="draft",
        status="approved",
    )
    stored_plan = artifact_store.require(stored_plan_ref)
    sql_artifact = SimpleNamespace(
        id="sql-1",
        version=1,
        content={"tables": ["cohort"], "origin": "test"},
        provenance={"study_id": "cohort-study"},
    )
    sql = 'SELECT "age" FROM cohort'
    execution = SqlExecutionResult(
        answer="",
        sql=sql,
        dataframe=pd.DataFrame({"age": [42]}),
        source_tables=["cohort"],
    )
    captured: dict[str, object] = {}
    real_persist = db_rag_tools.db_rag_persistence.persist_sql_subset_artifact

    def record_scope(*args, **kwargs):
        captured["runtime_root"] = kwargs.get("runtime_root")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(
        db_rag_tools.db_rag_persistence,
        "persist_sql_subset_artifact",
        record_scope,
    )

    result = db_rag_tools._persist_extraction_result(
        {"plan_id": stored_plan.id, "plan_version": stored_plan.version},
        context,
        stored_plan=stored_plan,
        plan=plan,
        sql_artifact=sql_artifact,
        validated=ValidatedExtractionSql(sql=sql, sha256="test-sha"),
        execution=execution,
    )

    assert captured["runtime_root"] is scope
    dataset = artifact_store.require(result.artifacts[0])
    assert dataset.content["path"].startswith(str(scope.datasets))
    assert dataset.provenance["study_id"] == "cohort-study"
