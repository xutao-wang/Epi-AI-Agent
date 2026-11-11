from __future__ import annotations

from itertools import combinations
import json
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
import pandas as pd
import pytest

from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    _execute_tools,
    _prepare_model_request,
    _tool_failure_recovery_required,
)
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import RunCustomPythonTool
from utils.model_runtime_profiles import model_runtime_profile


DATASET_FIELDS = (
    "dataset_id", "dataset_kind", "dataset_version",
    "dataset_source", "dataset_source_reason",
)


@pytest.fixture
def analysis(tmp_path):
    store = StateArtifactStore()
    path = tmp_path / "measurements.parquet"
    pd.DataFrame({"id": [1, 2, 3], "value": [2.0, 4.0, 6.0]}).to_parquet(path, index=False)
    store.save_dataset({
        "id": "measurements", "kind": "subset", "version": 1,
        "status": "active", "path": str(path),
    })
    studies = StudyRegistry()
    registry = ToolRegistry([RunCustomPythonTool(
        LocalPythonRuntime(runtime_root=tmp_path, memory_limit_bytes=None),
        runtime_root=tmp_path,
    )])
    context = ToolContext(studies=studies, artifact_store=store, thread_id="thread", policy=None)
    config = EpiAgentRuntimeConfig(
        agent_name="epi_agent", system_prompt="Use the selected input.",
        registry=registry, studies=studies,
        context_factory=lambda _state, _config, artifact_store: ToolContext(
            studies=studies, artifact_store=artifact_store, thread_id="thread", policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )
    args = {
        "analysis_goal": "Calculate the mean measurement.",
        "code": "print(dataset.value.mean())",
        "code_summary": "Compute the arithmetic mean.",
        "code_assumptions": "The selected measurements are complete.",
        "dataset_id": "measurements", "dataset_kind": "subset", "dataset_version": 1,
        "dataset_source": "prior_artifact", "dataset_source_reason": "Selected measurement records.",
    }
    return registry, context, config, args


def invoke(analysis, state, args, call_id):
    _, _, config, _ = analysis
    state.setdefault("messages", []).append(AIMessage(content="", tool_calls=[{
        "name": "analysis-run_custom_python", "args": args, "id": call_id, "type": "tool_call",
    }]))
    patch = _execute_tools(state, {"configurable": {"thread_id": "thread"}}, agent_config=config)
    messages = state["messages"] + patch.pop("messages")
    state.update(patch)
    state["messages"] = messages
    return messages[-1]


@pytest.mark.parametrize("provided", [
    fields for size in range(1, len(DATASET_FIELDS))
    for fields in combinations(DATASET_FIELDS, size)
])
def test_partial_reference_identifies_exact_missing_arguments(analysis, provided):
    registry, context, _, complete = analysis
    args = {k: v for k, v in complete.items() if k not in DATASET_FIELDS or k in provided}
    with pytest.raises(ToolExecutionError) as rejected:
        registry.invoke("analysis-run_custom_python", args, context=context)
    error = rejected.value
    assert error.code == "DATASET_METADATA_INCOMPLETE"
    assert error.recoverable
    expected = [field for field in DATASET_FIELDS if field not in provided]
    assert error.details["missing_fields"] == expected
    assert error.details["repair_fields"] == expected
    assert error.details["dataset_input_required"] is True
    assert error.details["execution_started"] is False
    assert "omit" not in str(error)
    assert not any(a.kind == "analysis_run" for a in context.artifact_store.list_artifacts())


def test_null_reference_fields_receive_the_same_targeted_feedback(analysis):
    registry, context, _, args = analysis
    with pytest.raises(ToolExecutionError) as rejected:
        registry.invoke("analysis-run_custom_python", {
            **args, "dataset_id": None, "dataset_kind": None, "dataset_version": None,
        }, context=context)
    assert rejected.value.details["missing_fields"] == ["dataset_id", "dataset_kind", "dataset_version"]


def test_metadata_error_is_forwarded_with_a_specific_repair_instruction(analysis):
    _, context, config, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    args = {k: v for k, v in complete.items() if k not in DATASET_FIELDS[:3]}
    message = invoke(analysis, state, args, "partial")
    error = json.loads(message.content)["error"]
    assert message.tool_call_id == "partial"
    assert error["details"]["missing_fields"] == list(DATASET_FIELDS[:3])
    assert "code" in error["details"]["instruction"]
    prepared = _prepare_model_request(state, agent_config=config)
    assert isinstance(prepared, tuple)
    assert any(isinstance(m, ToolMessage) and m.content == message.content for m in prepared[3])


def test_missing_arguments_can_be_repaired_without_changing_code(analysis):
    _, context, _, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    partial = {k: v for k, v in complete.items() if k not in DATASET_FIELDS[:3]}
    assert invoke(analysis, state, partial, "partial").status == "error"
    message = invoke(analysis, state, complete, "corrected")
    assert message.status == "success"
    store = StateArtifactStore.from_state(state)
    [run] = [a for a in store.list_artifacts() if a.kind == "analysis_run"]
    assert run.content["output_text"].strip() == "4.0"
    assert run.content["specification"]["code"] == partial["code"]
    assert run.content["dataset"] == {"id": "measurements", "kind": "subset", "version": 1}


def test_changing_code_does_not_reset_repeated_metadata_failure(analysis):
    _, context, _, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    partial = {k: v for k, v in complete.items() if k not in DATASET_FIELDS[:3]}
    invoke(analysis, state, partial, "partial")
    invoke(analysis, state, {**partial, "code": "print(dataset.value.sum())"}, "wrong-repair")
    assert len(state["failure_signatures"]) == 2
    assert _tool_failure_recovery_required(state["failure_signatures"])
    assert json.loads(state["failure_signatures"][-1])["arguments"]["code"] == "print(dataset.value.sum())"


def test_partial_argument_progress_remains_a_new_failure(analysis):
    _, context, _, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    partial = {k: v for k, v in complete.items() if k not in DATASET_FIELDS[:3]}
    invoke(analysis, state, partial, "partial")
    message = invoke(analysis, state, {**partial, "dataset_id": complete["dataset_id"]}, "progress")
    assert json.loads(message.content)["error"]["details"]["missing_fields"] == ["dataset_kind", "dataset_version"]
    assert not _tool_failure_recovery_required(state["failure_signatures"])
    assert invoke(analysis, state, complete, "corrected").status == "success"


def test_standalone_execution_needs_no_invented_reference(analysis):
    _, context, _, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    args = {k: v for k, v in complete.items() if k not in DATASET_FIELDS}
    args["code"] = "print(6 * 7)"
    assert invoke(analysis, state, args, "standalone").status == "success"
    store = StateArtifactStore.from_state(state)
    [run] = [a for a in store.list_artifacts() if a.kind == "analysis_run"]
    assert run.content["dataset"] is None
    assert run.content["output_text"].strip() == "42"


def test_standalone_substep_remains_possible_after_a_partial_reference(analysis):
    _, context, _, complete = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    partial = {k: v for k, v in complete.items() if k not in DATASET_FIELDS[:3]}
    invoke(analysis, state, partial, "partial")
    args = {k: v for k, v in complete.items() if k not in DATASET_FIELDS}
    args.update(analysis_goal="Check a dependency version.", code="import pandas as pd\nprint(pd.__version__)")
    assert invoke(analysis, state, args, "probe").status == "success"


def test_stale_reference_is_not_replaced_by_an_available_dataset(analysis):
    registry, context, _, args = analysis
    with pytest.raises(ToolExecutionError) as rejected:
        registry.invoke("analysis-run_custom_python", {**args, "dataset_version": 99}, context=context)
    assert rejected.value.code == "DATASET_NOT_AVAILABLE"


def test_current_upload_still_uses_its_exact_reference(analysis):
    registry, context, _, args = analysis
    source = context.artifact_store.require("measurements")
    context.artifact_store.save_dataset({
        **source.content, "id": "uploaded-measurements",
        "provenance": {"source_attachment_ids": ["upload-1"]},
    })
    upload_context = ToolContext(
        studies=context.studies, artifact_store=context.artifact_store,
        thread_id=context.thread_id, policy=None, current_attachment_ids=("upload-1",),
    )
    result = registry.invoke("analysis-run_custom_python", {
        **args, "dataset_id": "uploaded-measurements", "dataset_source": "current_upload",
    }, context=upload_context)
    run = context.artifact_store.require(result.artifacts[-1])
    assert run.content["dataset"]["id"] == "uploaded-measurements"
    assert run.content["output_text"].strip() == "4.0"


def test_source_mismatch_remains_an_error(analysis):
    registry, context, _, args = analysis
    with pytest.raises(ToolExecutionError) as rejected:
        registry.invoke("analysis-run_custom_python", {
            **args, "dataset_source": "current_upload",
        }, context=context)
    assert rejected.value.code == "DATASET_SOURCE_MISMATCH"


def test_tool_schema_keeps_the_existing_required_arguments(analysis):
    registry, _, _, _ = analysis
    schema = registry.spec("analysis-run_custom_python").model_schema()
    assert schema["function"]["parameters"]["required"] == [
        "analysis_goal", "code", "code_summary", "code_assumptions",
    ]


def test_other_errors_keep_the_existing_repetition_behavior(analysis):
    _, context, _, args = analysis
    state = {"artifacts": context.artifact_store.snapshot()}
    message = invoke(analysis, state, {**args, "dataset_version": 99}, "stale")
    assert json.loads(message.content)["error"]["code"] == "DATASET_NOT_AVAILABLE"
    message = invoke(analysis, state, {
        **args, "dataset_version": 99, "code": "print(dataset.value.sum())",
    }, "changed-stale")
    assert json.loads(message.content)["error"]["code"] == "DATASET_NOT_AVAILABLE"
    assert not _tool_failure_recovery_required(state["failure_signatures"])
