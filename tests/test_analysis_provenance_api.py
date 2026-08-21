from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.auth import LOCAL_SESSION_ID
from api.runtime import ApiGraphRunner, ReportAgentApiRuntime
from api.server import create_app
from epi_agent.analysis_artifacts import AnalysisRun, ArtifactIdentity, save_analysis_run
from epi_agent.artifacts import StateArtifactStore


DEFAULT_SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 300,
    "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
    "db_rag_reranker_model": "disabled",
}


class SnapshotGraph:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get_state(self, _config, subgraphs=True):
        del subgraphs
        return SimpleNamespace(values=self.values, next=(), interrupts=[])


def _runtime(
    store: StateArtifactStore,
    *,
    runtime_root: str | None = None,
) -> ReportAgentApiRuntime:
    graph = SnapshotGraph({"artifacts": store.snapshot()})
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda _settings, _context: graph,
        default_runtime_settings=DEFAULT_SETTINGS,
        models=["gpt-5.4"],
        runtime_root=runtime_root,
    )
    thread = runtime._thread("thread-1")
    thread.app = graph
    thread.runner = ApiGraphRunner(graph)
    return runtime


def _store(*, analysis_status: str = "active") -> StateArtifactStore:
    store = StateArtifactStore({})
    sql_ref = store.save_artifact(
        kind="validated_sql",
        status="approved",
        content={
            "status": "validated",
            "sql": 'SELECT "AGE" FROM "Index Baseline"',
            "sql_sha256": hashlib.sha256(
                b'SELECT "AGE" FROM "Index Baseline"'
            ).hexdigest(),
        },
    )
    store.save_dataset(
        {
            "id": "dataset-1",
            "kind": "analysis_dataset",
            "version": 1,
            "status": "active",
            "provenance": {
                "sql": 'SELECT "AGE" FROM "Index Baseline"',
                "sql_id": sql_ref.id,
                "sql_version": sql_ref.version,
            },
        }
    )
    run_ref = save_analysis_run(
        store,
        AnalysisRun(
            method="custom_python",
            dataset=ArtifactIdentity(
                id="dataset-1",
                kind="analysis_dataset",
                version=1,
            ),
            specification={
                "code": "print('exact analysis')",
                "dataset_source": "current_upload",
                "dataset_source_reason": "The table contains the requested fields.",
            },
            output_text="exact output",
            runtime={"language": "python"},
        ),
        thread_id="thread-1",
        status=analysis_status,
    )
    return store


def _analysis_id(store: StateArtifactStore) -> str:
    return next(
        artifact_id
        for artifact_id, artifact in store.snapshot()["files"].items()
        if artifact.get("kind") == "analysis_run"
    )


def test_dataset_provenance_resolves_exact_validated_sql() -> None:
    result = _runtime(_store()).dataset_provenance("thread-1", "dataset-1")

    assert result.dataset_id == "dataset-1"
    assert result.dataset_version == 1
    assert result.sql == 'SELECT "AGE" FROM "Index Baseline"'
    assert result.sql_artifact.id
    assert result.sql_artifact.kind == "validated_sql"


def test_analysis_result_returns_exact_python_and_dataset_identity() -> None:
    store = _store()
    run_id = _analysis_id(store)

    result = _runtime(store).analysis_result("thread-1", run_id)

    assert result.python_code == "print('exact analysis')"
    assert result.output_text == "exact output"
    assert result.dataset.id == "dataset-1"
    assert result.dataset.version == 1
    assert result.dataset_source == "current_upload"
    assert result.dataset_source_reason == "The table contains the requested fields."


def test_analysis_result_rejects_unpublished_analysis() -> None:
    store = _store(analysis_status="cancelled")
    run_id = _analysis_id(store)

    with pytest.raises(KeyError):
        _runtime(store).analysis_result("thread-1", run_id)


def test_dataset_provenance_rejects_sql_mismatch() -> None:
    store = _store()
    dataset = store._artifacts["datasets"]["dataset-1"]
    dataset["provenance"]["sql"] = "SELECT different"

    with pytest.raises(ValueError, match="SQL"):
        _runtime(store).dataset_provenance("thread-1", "dataset-1")


def test_provenance_routes_expose_lineage_contracts(tmp_path) -> None:
    store = _store()
    runtime = _runtime(store, runtime_root=str(tmp_path))
    analysis_id = _analysis_id(store)
    client = TestClient(
        create_app(runtime=runtime, provider_api_key="test-provider-key"),
        headers={"X-Epi-Session-Id": LOCAL_SESSION_ID},
    )

    dataset_response = client.get(
        "/api/threads/thread-1/datasets/dataset-1/provenance"
    )
    analysis_response = client.get(
        f"/api/threads/thread-1/analysis-runs/{analysis_id}"
    )

    assert dataset_response.status_code == 200
    assert dataset_response.json()["sql"] == 'SELECT "AGE" FROM "Index Baseline"'
    assert dataset_response.json()["sql_artifact"]["kind"] == "validated_sql"
    assert analysis_response.status_code == 200
    assert analysis_response.json()["python_code"] == "print('exact analysis')"
    assert analysis_response.json()["dataset"]["id"] == "dataset-1"


def test_provenance_routes_do_not_expose_cancelled_analysis(tmp_path) -> None:
    store = _store(analysis_status="cancelled")
    runtime = _runtime(store, runtime_root=str(tmp_path))
    analysis_id = _analysis_id(store)
    client = TestClient(
        create_app(runtime=runtime, provider_api_key="test-provider-key"),
        headers={"X-Epi-Session-Id": LOCAL_SESSION_ID},
    )

    response = client.get(f"/api/threads/thread-1/analysis-runs/{analysis_id}")

    assert response.status_code == 404
