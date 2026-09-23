from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
import pytest
from pydantic import BaseModel, ConfigDict

from api.activity_store import SqliteActivityStore
from api.runtime import project_thread_state
from epi_agent.activity import NullActivitySink
from epi_agent.artifacts import StateArtifactStore
from epi_agent.protocol import ArtifactRef, ToolContext, ToolExecutionError, ToolResult, ToolSpec
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools, _prepare_model_request
from epi_agent.studies import StudyRegistry
from epi_agent.tool_tracking import ToolTracking
from utils.model_runtime_profiles import model_runtime_profile


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int


class _StrictTool:
    @property
    def spec(self):
        return ToolSpec(name="strict_tool", description="Test tool", args_model=Arguments)

    def invoke(self, arguments, context):
        return ToolResult(message="completed")


def _config(registry):
    studies = StudyRegistry()
    return EpiAgentRuntimeConfig(
        agent_name="test_agent", system_prompt="Use tools.",
        registry=registry, studies=studies,
        context_factory=lambda state, config, store: ToolContext(
            studies=studies, artifact_store=store, thread_id="thread-1", policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )


def _tool_call(arguments, call_id="call-1", tool_name="strict_tool"):
    return {"name": tool_name, "args": arguments, "id": call_id, "type": "tool_call"}


class Sink(NullActivitySink):
    def __init__(self):
        self.records = {}

    def tool_execution(self, thread_id, record):
        self.records[record["execution_id"]] = deepcopy(record)


def execute(tool=None, *, args=None, sink=None, name="strict_tool", extra_calls=None):
    tool = tool or _StrictTool()
    config = _config(ToolRegistry([tool]))
    if sink is not None:
        config = replace(config, activity_sink=sink)
    state = {"messages": [AIMessage(content="", tool_calls=[
        _tool_call(args if args is not None else {"limit": 1}, tool_name=name),
        *(extra_calls or []),
    ])], "artifacts": {}}
    before = deepcopy(state)
    patch = _execute_tools(state, {"configurable": {"thread_id": "thread-1"}}, agent_config=config)
    assert state == before
    return patch


@pytest.mark.parametrize("args", [{"limit": 1}, {"limit": "bad"}])
def test_tracking_does_not_change_messages_or_graph_state(args):
    sink = Sink()
    baseline = execute(args=args)
    tracked = execute(args=args, sink=sink)
    assert tracked == baseline
    config = _config(ToolRegistry([_StrictTool()]))
    assert _prepare_model_request(tracked, agent_config=config) == _prepare_model_request(baseline, agent_config=config)
    record, = sink.records.values()
    assert record["status"] == ("success" if args["limit"] == 1 else "error")
    assert record["arguments_hash"]
    assert "arguments" not in record
    assert "tool_executions" not in tracked


def test_broken_sink_does_not_change_execution():
    class Broken(Sink):
        def tool_execution(self, *args):
            raise OSError("disk unavailable")
    assert execute(sink=Broken()) == execute()


def test_unknown_tool_and_invalid_batch_are_recorded():
    sink = Sink()
    execute(sink=sink, name="unknown_tool")
    record, = sink.records.values()
    assert record["status"] == "error"
    assert record["error_code"] == "UNKNOWN_TOOL"
    class Mutating(_StrictTool):
        @property
        def spec(self):
            return replace(super().spec, read_only=False)
    sink = Sink()
    execute(Mutating(), sink=sink, extra_calls=[_tool_call({"limit": 2}, call_id="call-2")])
    assert len(sink.records) == 2
    assert all(r["status"] == "rejected" for r in sink.records.values())


@pytest.mark.parametrize("error", [RuntimeError("private detail"), ToolExecutionError("TEST_FATAL", "failed", recoverable=False)])
def test_terminal_error_records_skipped_calls(error):
    class Failing(_StrictTool):
        def invoke(self, *args):
            raise error
    sink = Sink()
    execute(Failing(), sink=sink, extra_calls=[_tool_call({"limit": 2}, call_id="call-2")])
    assert [r["status"] for r in sink.records.values()] == ["error", "skipped"]
    assert "private detail" not in str(sink.records)


def test_review_resume_gets_distinct_execution_ids():
    class Pausing(_StrictTool):
        def invoke(self, *args):
            raise GraphInterrupt(())
    sink = Sink()
    with pytest.raises(GraphInterrupt):
        execute(Pausing(), sink=sink)
    execute(sink=sink)
    records = list(sink.records.values())
    assert [r["status"] for r in records] == ["interrupted", "success"]
    assert records[0]["tool_call_id"] == records[1]["tool_call_id"]
    assert records[0]["execution_id"] != records[1]["execution_id"]


def test_artifact_access_and_creation_are_distinct_from_returning_reference():
    store = StateArtifactStore({"datasets": {"old": {"id": "old", "version": 1}}})
    sink = Sink()
    tracking = ToolTracking(sink, "thread-1", _tool_call({"limit": 1}), store)
    with tracking.capture_reads():
        store.require("old")
    store.save_dataset({"id": "new", "version": 1})
    tracking.finish("success", result=ToolResult(message="ok", artifacts=[ArtifactRef("old", "dataset", 1)]))
    record, = sink.records.values()
    assert [r["id"] for r in record["accessed_artifacts"]] == ["old"]
    assert [r["id"] for r in record["created_artifacts"]] == ["new"]
    assert [r["id"] for r in record["returned_artifacts"]] == ["old"]


def test_version_and_status_changes_are_updates():
    store = StateArtifactStore()
    ref = store.save_artifact(kind="dataset_plan", content={}, status="draft")
    tracking = ToolTracking(sink := Sink(), "thread-1", _tool_call({"limit": 1}), store)
    store.transition_artifact_status(ref, expected_status="draft", status="approved")
    tracking.finish("success")
    record, = sink.records.values()
    assert record["created_artifacts"] == []
    assert record["updated_artifacts"] == [{"id": ref.id, "kind": ref.kind, "version": ref.version, "status": "approved"}]


def test_sqlite_persistence_projection_and_cascade(tmp_path):
    store = SqliteActivityStore(tmp_path / "activity.sqlite")
    store.start_run("thread-1", "message-1")
    execute(sink=store)
    records = store.list_tool_executions("thread-1")
    assert len(records) == 1
    assert records[0]["status"] == "success"
    store = SqliteActivityStore(tmp_path / "activity.sqlite")
    projected = project_thread_state(thread_id="thread-1", snapshot=None, run_status={}, tool_executions=records)
    assert projected.tool_tracking_version == "tool-execution-v1"
    assert projected.tool_executions == records
    assert projected.conversation == []
    store.delete_thread("thread-1")
    assert store.list_tool_executions("thread-1") == []


def test_missing_dependency_projects_a_concise_public_run_failure():
    snapshot = SimpleNamespace(
        values={
            "messages": [],
            "artifacts": {},
            "terminal_error": {
                "code": "DEPENDENCY_NOT_AVAILABLE",
                "message": "Package “sklearn” isn’t installed.",
                "recoverable": False,
            },
        },
        next=(),
        interrupts=(),
    )

    projected = project_thread_state(
        thread_id="thread-1",
        snapshot=snapshot,
        run_status={"state": "done", "steps": 1},
    )

    assert projected.run.state == "error"
    assert projected.run.error_code == "DEPENDENCY_NOT_AVAILABLE"
    assert projected.run.user_message == "Package “sklearn” isn’t installed."
