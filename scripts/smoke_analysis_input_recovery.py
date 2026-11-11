#!/usr/bin/env python3.12
"""Exercise dataset argument repair through the real native executor and worker.

This internal contract smoke supplies controlled tool calls directly. It uses
real artifact storage, tool dispatch, error serialization, execution history,
SQLite activity tracking and the Python worker. It invokes no LLM and makes no
claim about a model's probability of repairing its arguments.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import traceback

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
import pandas as pd

from api.activity_store import SqliteActivityStore
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools, _prepare_model_request
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import RunCustomPythonTool
from utils.model_runtime_profiles import model_runtime_profile


def main() -> int:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="epi-analysis-input-recovery-smoke-"))
    result_path = root / "result.json"
    result = {
        "status": "running", "started_at": datetime.now(UTC).isoformat(),
        "artifacts": str(root), "model_invoked": False, "checks": [],
    }

    def timeout(*_):
        raise TimeoutError("Five-minute smoke limit exceeded")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(300)
    try:
        store = StateArtifactStore()
        path = root / "measurements.parquet"
        pd.DataFrame({"id": [1, 2, 3], "value": [2.0, 4.0, 6.0]}).to_parquet(path, index=False)
        store.save_dataset({"id": "measurements", "kind": "subset", "version": 1,
            "status": "active", "path": str(path)})
        store.save_dataset({"id": "uploaded-measurements", "kind": "subset", "version": 1,
            "status": "active", "path": str(path),
            "provenance": {"source_attachment_ids": ["upload-1"]}})
        studies = StudyRegistry()
        activity = SqliteActivityStore(root / "activity.sqlite")
        registry = ToolRegistry([RunCustomPythonTool(
            LocalPythonRuntime(runtime_root=root, memory_limit_bytes=None), runtime_root=root,
        )])
        config = EpiAgentRuntimeConfig(
            agent_name="epi_agent", system_prompt="Use the selected exact dataset reference.",
            registry=registry, studies=studies, activity_sink=activity,
            context_factory=lambda _state, _config, artifacts: ToolContext(
                studies=studies, artifact_store=artifacts, thread_id="smoke-thread",
                policy=None, current_attachment_ids=("upload-1",),
            ),
            model_profile=model_runtime_profile("gpt-5.4"),
        )
        complete = {
            "analysis_goal": "Calculate the arithmetic mean of the selected measurements.",
            "code": "print(dataset.value.mean())",
            "code_summary": "Calculate the mean.", "code_assumptions": "Complete measurements.",
            "dataset_id": "measurements", "dataset_kind": "subset", "dataset_version": 1,
            "dataset_source": "prior_artifact", "dataset_source_reason": "Selected measurement records.",
        }
        missing = ["dataset_id", "dataset_kind", "dataset_version"]
        partial = {key: value for key, value in complete.items() if key not in missing}
        fields = [*missing, "dataset_source", "dataset_source_reason"]

        def fresh_state():
            return {"messages": [], "artifacts": store.snapshot()}

        def invoke(state, args, call_id):
            activity.start_run("smoke-thread", call_id)
            state["messages"].append(AIMessage(content="", tool_calls=[{
                "name": "analysis-run_custom_python", "args": args, "id": call_id, "type": "tool_call",
            }]))
            patch = _execute_tools(state, {"configurable": {"thread_id": "smoke-thread"}}, agent_config=config)
            messages = state["messages"] + patch.pop("messages")
            state.update(patch)
            state["messages"] = messages
            activity.finish("smoke-thread", "completed")
            return messages[-1]

        state = fresh_state()
        message = invoke(state, partial, "partial-reference")
        error = json.loads(message.content)["error"]
        assert error["code"] == "DATASET_METADATA_INCOMPLETE"
        assert error["details"]["missing_fields"] == missing
        assert error["details"]["repair_fields"] == missing
        assert error["details"]["execution_started"] is False
        assert "omit" not in error["message"]
        prepared = _prepare_model_request(state, agent_config=config)
        assert isinstance(prepared, tuple)
        assert any(isinstance(m, ToolMessage) and m.content == message.content for m in prepared[3])
        result["first_model_facing_error"] = error
        result["checks"].append("Targeted missing-field feedback is retained in the next model request.")

        assert invoke(state, complete, "corrected-reference").status == "success"
        runs = [a for a in StateArtifactStore.from_state(state).list_artifacts() if a.kind == "analysis_run"]
        assert len(runs) == 1
        assert runs[0].content["output_text"].strip() == "4.0"
        assert runs[0].content["specification"]["code"] == partial["code"]
        assert runs[0].content["dataset"] == {"id": "measurements", "kind": "subset", "version": 1}
        result["checks"].append("Correcting the reference executes the unchanged code on the exact generated dataset.")

        repeated = fresh_state()
        invoke(repeated, partial, "repeat-initial")
        invoke(repeated, {**partial, "code": "print(dataset.value.sum())"}, "code-only-repair")
        prepared = _prepare_model_request(repeated, agent_config=config)
        assert isinstance(prepared, tuple)
        assert any(isinstance(m, SystemMessage) and "TOOL_FAILURE_RECOVERY" in m.content for m in prepared[3])
        assert not any(a.kind == "analysis_run" for a in StateArtifactStore.from_state(repeated).list_artifacts())
        result["checks"].append("Code-only edits reach the existing repeated-failure stop without Python execution.")

        standalone = fresh_state()
        args = {key: value for key, value in complete.items() if key not in fields}
        args.update(analysis_goal="Perform standalone arithmetic.", code="print(6 * 7)")
        assert invoke(standalone, args, "standalone").status == "success"
        [run] = [a for a in StateArtifactStore.from_state(standalone).list_artifacts() if a.kind == "analysis_run"]
        assert run.content["dataset"] is None and run.content["output_text"].strip() == "42"
        result["checks"].append("Standalone arithmetic executes without inventing or selecting a dataset.")

        uploaded = fresh_state()
        assert invoke(uploaded, {**complete, "dataset_id": "uploaded-measurements",
            "dataset_source": "current_upload"}, "upload").status == "success"
        [run] = [a for a in StateArtifactStore.from_state(uploaded).list_artifacts() if a.kind == "analysis_run"]
        assert run.content["dataset"]["id"] == "uploaded-measurements"
        assert run.content["output_text"].strip() == "4.0"
        result["checks"].append("Current-upload source validation and execution remain functional.")

        stale = fresh_state()
        error = json.loads(invoke(stale, {**complete, "dataset_version": 99}, "stale").content)["error"]
        assert error["code"] == "DATASET_NOT_AVAILABLE"
        result["checks"].append("An unavailable version remains rejected without selecting a replacement.")
        for name, snapshot in [("repaired", state), ("repeated", repeated), ("standalone", standalone), ("uploaded", uploaded), ("stale", stale)]:
            (root / f"{name}-state.json").write_text(json.dumps({
                **snapshot, "messages": [message.model_dump(mode="json") for message in snapshot["messages"]],
            }, indent=2, default=str) + "\n")
        result["status"] = "passed"
    except BaseException:
        result.update(status="failed", traceback=traceback.format_exc())
    finally:
        signal.alarm(0)
        result["duration_seconds"] = time.monotonic() - started
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        print(f"{result['status']}: {result_path}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
