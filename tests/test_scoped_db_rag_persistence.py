from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from epi_agent.db_rag.persistence import persist_sql_subset_artifact
from graph.state import MetaKeys
from utils.dataset_artifacts import load_dataset_artifact
from utils.user_storage import UserStorageLayout


def test_sql_subset_persistence_uses_authorized_thread_dataset_root(tmp_path) -> None:
    scope = UserStorageLayout(tmp_path).thread("cognito-user-a", "thread-1")
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
        selection_artifact_id="plan-1",
        sql_candidate_artifact_id="sql-1",
        dataset_id="subset-1",
        runtime_root=scope,
    )

    assert artifact["path"] == str(scope.datasets / "subset-1.parquet")
    dataframe, _schema = load_dataset_artifact(artifact, runtime_root=scope)
    assert dataframe.to_dict(orient="records") == [{"age": 42}]
