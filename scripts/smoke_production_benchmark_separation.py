#!/usr/bin/env python3.12
"""Real internal production smoke, with no external model or service invocation.

Retains the persisted input, SQLite activity, LangGraph state, API projection,
and report in a unique temporary directory. Runs once with a five-minute limit.
"""
from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import traceback

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pandas as pd

from api.activity_store import SqliteActivityStore
from api.runtime import ReportAgentApiRuntime, project_thread_state
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools, _prepare_model_request
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.runtimes.python.models import PythonExecutionRequest
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import RunCustomPythonTool
from utils.model_runtime_profiles import model_runtime_profile
from utils.user_storage import ThreadStorageScope


def main() -> int:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="epi-production-separation-smoke-"))
    report = {"status": "running", "started_at": datetime.now(UTC).isoformat(),
              "output_directory": str(root), "model_invoked": False, "checks": []}
    state = {}
    activity = None

    def timeout(*_args):
        raise TimeoutError("Five-minute smoke limit exceeded")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(300)
    try:
        scope = ThreadStorageScope(root=root, owner_user_id="smoke-owner", thread_id="smoke-thread")
        scope.datasets.mkdir()
        path = scope.datasets / "measurements.parquet"
        frame = pd.DataFrame({"value": pd.Series(range(100_000), dtype="Int64"),
                              "text": pd.Series((["001", "002", "NA", "null", "", None] * 16_667)[:100_000], dtype="string"),
                              "nullable": pd.Series(([1.5, None] * 50_000), dtype="Float64")})
        frame.to_parquet(path, index=False)
        store = StateArtifactStore()
        store.save_dataset({"id": "measurements", "kind": "subset", "version": 1,
                            "status": "active", "path": str(path)})
        studies = StudyRegistry()
        activity = SqliteActivityStore(root / "activity.sqlite")
        activity.start_run(scope.thread_id, "smoke-user-message")
        python = LocalPythonRuntime(runtime_root=scope.execution, memory_limit_bytes=None)
        tool = RunCustomPythonTool(python, runtime_root=root)
        registry = ToolRegistry([tool])
        agent_config = EpiAgentRuntimeConfig(
            agent_name="epi_agent", system_prompt="Use the exact selected input.",
            registry=registry, studies=studies, activity_sink=activity,
            context_factory=lambda _state, _config, artifacts: ToolContext(
                studies=studies, artifact_store=artifacts, thread_id=scope.thread_id,
                policy=None, thread_storage=scope),
            model_profile=model_runtime_profile("gpt-5.4"))

        def execute_node(values, config: RunnableConfig):
            patch = _execute_tools(values, config, agent_config=agent_config)
            return {**values, **patch, "messages": [*values.get("messages", []), *patch["messages"]]}

        builder = StateGraph(dict)
        builder.add_node("execute", execute_node)
        builder.add_edge(START, "execute")
        builder.add_edge("execute", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        runnable_config = {"configurable": {"thread_id": scope.thread_id}}
        state = {"messages": [], "artifacts": store.snapshot()}
        code = """assert dataset.text.iloc[:5].tolist() == ['001', '002', 'NA', 'null', '']
assert dataset.text.isna().iloc[5]
assert str(dataset.value.dtype) == 'Int64'
assert str(dataset.nullable.dtype) == 'Float64'
assert dataset.nullable.isna().sum() == 50000
print(len(dataset), dataset.value.mean())"""
        complete = {"analysis_goal": "Count observations and compute the mean.", "code": code,
                    "code_summary": "Validate typed input and compute summary.", "code_assumptions": "One row per observation.",
                    "dataset_id": "measurements", "dataset_kind": "subset", "dataset_version": 1,
                    "dataset_source": "prior_artifact", "dataset_source_reason": "Selected observations."}

        def dispatch(arguments, call_id):
            nonlocal state
            state["messages"].append(AIMessage(content="", tool_calls=[{
                "name": tool.spec.name, "args": arguments, "id": call_id, "type": "tool_call"}]))
            state = graph.invoke(state, runnable_config)
            return state["messages"][-1]

        missing = ["dataset_id", "dataset_kind", "dataset_version"]
        partial = {key: value for key, value in complete.items() if key not in missing}
        rejected = dispatch(partial, "partial-reference")
        assert rejected.status == "error"
        error = json.loads(rejected.content)["error"]
        assert error["code"] == "DATASET_METADATA_INCOMPLETE"
        assert error["details"]["missing_fields"] == missing
        assert error["details"]["repair_fields"] == missing
        assert error["details"]["execution_started"] is False
        assert not any(a.kind == "analysis_run" for a in StateArtifactStore.from_state(state).list_artifacts())
        prepared = _prepare_model_request(state, agent_config=agent_config)
        assert isinstance(prepared, tuple)
        assert any(isinstance(message, ToolMessage) and message.content == rejected.content for message in prepared[3])
        report["checks"].append("Metadata error returns exact missing fields before worker execution and survives request preparation.")
        for index in range(5):
            assert dispatch(complete, f"repaired-read-{index}").status == "success"
        runs = [a for a in StateArtifactStore.from_state(state).list_artifacts() if a.kind == "analysis_run"]
        assert len(runs) == 5
        for run in runs:
            assert run.status == "pending_review"
            assert run.content["output_text"].strip() == "100000 49999.5"
            assert run.content["dataset"] == {"id": "measurements", "kind": "subset", "version": 1}
            assert run.content["specification"]["code"] == partial["code"]
            assert "analytic_datasets" not in run.content
        report["checks"].append("Five real workers preserve text/null/numeric types, compute the mean and stage ordinary review artifacts with exact input provenance.")
        hook = python.execute(PythonExecutionRequest(code="try:\n    record_analysis_dataset\nexcept NameError:\n    print('disabled')\nelse:\n    print('enabled')"), {})
        assert hook.output_text.strip() == "disabled"
        assert "record_analysis_dataset" not in json.dumps(tool.spec.model_schema())
        for method in ("benchmark_manifest", "benchmark_evidence"):
            assert not hasattr(ReportAgentApiRuntime, method)
        for module in ("api.benchmark_evidence", "utils.evidence_files", "utils.evidence_redaction"):
            assert importlib.util.find_spec(module) is None
        report["checks"].append("Worker recording hook, schema instructions and benchmark exporters are absent.")
        records = activity.list_tool_executions(scope.thread_id)
        assert len(records) == 6
        assert records[0]["error_code"] == "DATASET_METADATA_INCOMPLETE"
        assert all(record["schema_version"] == "tool-execution-v1" for record in records)
        assert all(not ({"artifact_snapshots", "arguments", "result", "error"} & record.keys()) for record in records)
        raw_tracking = json.dumps(records, indent=2)
        assert len(raw_tracking.encode()) < 50_000
        activity.finish(scope.thread_id, "completed")
        projected = project_thread_state(
            thread_id=scope.thread_id, snapshot=graph.get_state(runnable_config),
            run_status={"state": "done"}, activity_runs=activity.list_runs(scope.thread_id), tool_executions=records)
        raw_api = projected.model_dump_json(indent=2)
        assert len(raw_api.encode()) < 100_000
        assert projected.tool_executions == records
        (root / "api-state.json").write_text(raw_api + "\n")
        report.update(tracking_bytes=len(raw_tracking.encode()), api_state_bytes=len(raw_api.encode()), status="passed")
        report["checks"].append("SQLite provenance and actual LangGraph API projection stay within production payload bounds.")
    except BaseException:
        report.update(status="failed", traceback=traceback.format_exc())
    finally:
        signal.alarm(0)
        try:
            (root / "raw-state.json").write_text(json.dumps(state, indent=2, default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)) + "\n")
            if activity is not None:
                (root / "tracking.json").write_text(json.dumps(activity.list_tool_executions("smoke-thread"), indent=2) + "\n")
        except BaseException:
            report.update(status="failed", diagnostic_export_traceback=traceback.format_exc())
        report["duration_seconds"] = time.monotonic() - started
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"{report['status']}: {root / 'report.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
