"""Production contracts: bounded provenance without evaluation payload capture."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pandas as pd

from api.activity_store import SqliteActivityStore
from api.runtime import ReportAgentApiRuntime, project_thread_state
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.runtimes.python.models import PythonExecutionRequest
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import RunCustomPythonTool
from utils.model_runtime_profiles import model_runtime_profile
from utils.user_storage import ThreadStorageScope


def test_five_real_dataset_reads_keep_sqlite_and_api_provenance_bounded(tmp_path):
    path = tmp_path / "measurements.parquet"
    pd.DataFrame({"id": range(100_000), "value": range(100_000),
                  "label": [f"dataset-content-marker-{i:06d}" for i in range(100_000)]}).to_parquet(path, index=False)
    store = StateArtifactStore()
    store.save_dataset({"id": "measurements", "kind": "subset", "version": 1,
                        "status": "active", "path": str(path)})
    activity = SqliteActivityStore(tmp_path / "activity.sqlite")
    activity.start_run("thread", "user-message")
    studies = StudyRegistry()
    registry = ToolRegistry([RunCustomPythonTool(
        LocalPythonRuntime(runtime_root=tmp_path, memory_limit_bytes=None), runtime_root=tmp_path)])
    config = EpiAgentRuntimeConfig(
        agent_name="epi_agent", system_prompt="Analyze the exact dataset.",
        registry=registry, studies=studies, activity_sink=activity,
        context_factory=lambda _state, _config, artifacts: ToolContext(
            studies=studies, artifact_store=artifacts, thread_id="thread", policy=None,
            thread_storage=ThreadStorageScope(root=tmp_path, owner_user_id="owner", thread_id="thread")),
        model_profile=model_runtime_profile("gpt-5.4"))
    args = {"analysis_goal": "Count selected rows.", "code": "print(len(dataset))",
            "code_summary": "Count records.", "code_assumptions": "Each row is one observation.",
            "dataset_id": "measurements", "dataset_kind": "subset", "dataset_version": 1,
            "dataset_source": "prior_artifact", "dataset_source_reason": "Selected observations."}
    state = {"artifacts": store.snapshot()}
    for index in range(5):
        state["messages"] = [AIMessage(content="", tool_calls=[{
            "name": "analysis-run_custom_python", "args": args, "id": f"read-{index}", "type": "tool_call"}])]
        patch = _execute_tools(state, {"configurable": {"thread_id": "thread"}}, agent_config=config)
        assert patch["messages"][-1].status == "success"
        state.update(patch)
    records = activity.list_tool_executions("thread")
    assert len(records) == 5
    assert all(item["accessed_artifacts"] for item in records)
    encoded = json.dumps(records).encode()
    assert len(encoded) < 50_000

    def assert_no_copies(value):
        if isinstance(value, dict):
            assert not ({"artifact_snapshots", "arguments", "result", "error", "content_base64", "data_base64"} & value.keys())
            for item in value.values():
                assert_no_copies(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_copies(item)

    assert_no_copies(records)
    assert b"dataset-content-marker" not in encoded
    assert all(item["schema_version"] == "tool-execution-v1" for item in records)
    projected = project_thread_state(
        thread_id="thread", snapshot=SimpleNamespace(values=state, next=(), interrupts=[]),
        run_status={"state": "done"}, activity_runs=activity.list_runs("thread"), tool_executions=records)
    assert len(projected.model_dump_json().encode()) < 100_000
    assert projected.tool_executions == records
    runs = [item for item in StateArtifactStore.from_state(state).list_artifacts() if item.kind == "analysis_run"]
    assert len(runs) == 5
    assert all(run.status == "pending_review" for run in runs)
    assert all(run.content["dataset"] == {"id": "measurements", "kind": "subset", "version": 1} for run in runs)
    assert all(run.content["output_text"].strip() == "100000" for run in runs)
    assert all("analytic_datasets" not in run.content for run in runs)


def test_worker_has_no_recording_hook_and_tool_has_no_recording_instructions(tmp_path):
    runtime = LocalPythonRuntime(runtime_root=tmp_path, memory_limit_bytes=None)
    result = runtime.execute(PythonExecutionRequest(code="try:\n    record_analysis_dataset\nexcept NameError:\n    print('disabled')\nelse:\n    print('enabled')"), {})
    assert result.output_text.strip() == "disabled"
    schema = json.dumps(RunCustomPythonTool(runtime, runtime_root=tmp_path).spec.model_schema())
    assert "record_analysis_dataset" not in schema
    assert "analysis_datasets" not in result.model_dump()


def test_worker_transfer_preserves_text_nulls_and_nullable_numeric_types(tmp_path):
    frame = pd.DataFrame({
        "text": pd.Series(["001", "002", "NA", "null", "", None], dtype="string"),
        "integer": pd.Series([1, None, 3, 0, -1, 9], dtype="Int64"),
        "number": pd.Series([1.5, None, 3.0, 0.0, -1.0, 9.0], dtype="Float64"),
    })
    code = """assert str(dataset.text.dtype) == 'string'
assert dataset.text.iloc[:5].tolist() == ['001', '002', 'NA', 'null', '']
assert dataset.text.isna().tolist() == [False, False, False, False, False, True]
assert str(dataset.integer.dtype) == 'Int64'
assert str(dataset.number.dtype) == 'Float64'
assert dataset.integer.isna().tolist() == [False, True, False, False, False, False]
assert dataset.number.isna().tolist() == [False, True, False, False, False, False]
assert dataset.integer.dropna().tolist() == [1, 3, 0, -1, 9]
assert dataset.number.dropna().tolist() == [1.5, 3.0, 0.0, -1.0, 9.0]
print('preserved')"""
    result = LocalPythonRuntime(runtime_root=tmp_path, memory_limit_bytes=None).execute(
        PythonExecutionRequest(code=code, selected_dataset_id="typed"), {"typed": frame})
    assert result.output_text.strip() == "preserved"


def test_production_has_no_benchmark_exporter_or_runtime_methods():
    assert not hasattr(ReportAgentApiRuntime, "benchmark_manifest")
    assert not hasattr(ReportAgentApiRuntime, "benchmark_evidence")
    for module in ("api.benchmark_evidence", "utils.evidence_files", "utils.evidence_redaction"):
        assert importlib.util.find_spec(module) is None


def test_shipped_env_template_leaves_bedrock_catalog_policy_optional():
    import epi_agent
    root = Path(epi_agent.__file__).resolve().parents[1]
    settings = dict(line.split("=", 1) for line in (root / ".env.example").read_text().splitlines()
                    if line and not line.startswith("#") and "=" in line)
    assert not settings.get("BEDROCK_EXCLUDED_PROVIDERS", "").strip()
    assert not settings.get("BEDROCK_ALLOWED_MODELS", "").strip()


def test_docker_context_excludes_local_test_and_smoke_tooling():
    import epi_agent
    root = Path(epi_agent.__file__).resolve().parents[1]
    patterns = (root / ".dockerignore").read_text().splitlines()
    assert "tests" in patterns
    assert "scripts/smoke_*" in patterns
    assert "scripts/e2e_*" in patterns
    assert not any(line.strip() == "scripts" for line in patterns)
