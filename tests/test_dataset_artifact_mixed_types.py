from __future__ import annotations

import pandas as pd

from utils.dataset_artifacts import persist_dataset_artifact


def test_persist_dataset_artifact_accepts_mixed_numeric_and_text_column(tmp_path):
    dataframe = pd.DataFrame(
        {"Result": [1.2, 5, "Not done", None], "Site": ["a", "b", "c", "d"]}
    )
    persisted = persist_dataset_artifact(
        runtime_root=tmp_path,
        thread_id="thread-1",
        dataset_id="uploaded-mixed",
        kind="uploaded",
        dataframe=dataframe,
        schema={},
        provenance={},
    )
    stored = pd.read_parquet(persisted["path"])
    assert stored["Result"].tolist()[:3] == ["1.2", "5", "Not done"]
    assert pd.isna(stored["Result"].iloc[3])
    assert stored["Site"].tolist() == ["a", "b", "c", "d"]


def test_python_runtime_input_copy_uses_the_same_mixed_type_guard():
    # the sandbox input copy (local_process) writes parquet too; it must not fail on the same column
    import inspect

    from epi_agent.runtimes.python import local_process

    assert "parquet_safe_dataframe(dataframe).to_parquet" in inspect.getsource(local_process)
