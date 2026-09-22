from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from api.runtime import _initial_graph_state
from epi_agent.protocol import (
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    GenericEpiAgentState,
    build_epi_agent_graph,
)
from epi_agent.studies import StudyRegistry
from epi_agent.tool_call_protocol import follow_up_message_patch
from utils.model_runtime_profiles import model_runtime_profile


PYTHON_TOOL_NAME = "analysis-run_custom_python"
EVIDENCE_TOOL_NAME = "analysis-evidence"


class Arguments(BaseModel):
    code: str


def _policy_details(operation: str) -> dict[str, Any]:
    return {
        "category": "policy_blocked",
        "violations": [
            {
                "rule_id": "PY_FILESYSTEM_ACCESS",
                "operation": operation,
                "message": "Generated code cannot access or modify files.",
                "correction": "Analyze the preloaded dataset.",
                "line": 1,
                "column": 0,
            },
            {
                "rule_id": "PY_NETWORK_ACCESS",
                "operation": "requests",
                "message": "Generated code cannot access the network.",
                "correction": "Use evidence already supplied to the runtime.",
                "line": 2,
                "column": 0,
            },
        ],
        "violations_truncated": False,
    }


class PolicyPythonTool:
    spec = ToolSpec(
        name=PYTHON_TOOL_NAME,
        description="Exercise Python policy recovery.",
        args_model=Arguments,
        read_only=False,
    )

    def __init__(self) -> None:
        self.executions: list[dict[str, str]] = []

    def invoke(self, arguments, context):
        self.executions.append(arguments)
        code = arguments["code"]
        if code == "safe":
            return ToolResult(message="Corrected Python analysis completed.")
        if code == "syntax":
            raise ToolExecutionError(
                "INVALID_PYTHON_SYNTAX",
                "Generated Python contains invalid syntax.",
                recoverable=True,
                details={"category": "invalid_syntax", "line": 1, "column": 8},
            )
        raise ToolExecutionError(
            "POLICY_BLOCKED",
            "Generated Python violates the execution policy.",
            recoverable=True,
            details=_policy_details(code),
        )


class EvidenceTool:
    spec = ToolSpec(
        name=EVIDENCE_TOOL_NAME,
        description="Return permitted evidence.",
        args_model=Arguments,
    )

    def __init__(self) -> None:
        self.executions: list[dict[str, str]] = []

    def invoke(self, arguments, context):
        self.executions.append(arguments)
        return ToolResult(message="Evidence is available.")


class ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = iter(responses)
        self.bound_schema_names: list[tuple[str, ...]] = []
        self.message_batches: list[list[object]] = []

    def bind_tools(self, schemas):
        self.bound_schema_names.append(
            tuple(schema["function"]["name"] for schema in schemas)
        )
        return self

    def invoke(self, messages, **kwargs):
        self.message_batches.append(list(messages))
        return next(self.responses)


def tool_call(call_id: str, code: str, *, name: str = PYTHON_TOOL_NAME, **details):
    return {
        "name": name,
        "args": {"code": code, **details},
        "id": call_id,
        "type": "tool_call",
    }


def build_graph(python_tool, model, *, other_tools=()):
    studies = StudyRegistry()
    config = EpiAgentRuntimeConfig(
        agent_name="test",
        system_prompt="Use tools.",
        registry=ToolRegistry([python_tool, *other_tools]),
        studies=studies,
        context_factory=lambda state, config, store: ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )
    return build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        config=config,
        model=model,
        checkpointer=InMemorySaver(),
    )


def run_graph(python_tool, model, *, other_tools=()):
    graph = build_graph(python_tool, model, other_tools=other_tools)
    state = _initial_graph_state(
        "thread-1",
        HumanMessage(content="Analyze the approved dataset."),
    )
    result = graph.invoke(state, {"configurable": {"thread_id": "thread-1"}})
    return graph, result


def error_payloads(messages):
    return [
        json.loads(str(message.content))["error"]
        for message in messages
        if isinstance(message, ToolMessage) and message.status == "error"
    ]


def test_first_policy_block_allows_one_changed_safe_call():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("blocked", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("safe", "safe")]),
            AIMessage(content="The corrected Python analysis completed."),
        ]
    )

    _graph, result = run_graph(python_tool, model)

    first_error = error_payloads(result["messages"])[0]
    assert first_error["code"] == "POLICY_BLOCKED"
    assert first_error["details"]["repair_required"] is True
    assert first_error["details"]["repair_attempts_remaining"] == 1
    assert len(first_error["details"]["violations"]) == 2
    assert python_tool.executions == [{"code": "blocked-a"}, {"code": "safe"}]
    assert result["final_response"] == "The corrected Python analysis completed."


def test_unchanged_blocked_call_consumes_repair_without_reexecution():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("same", "blocked-a")]),
            AIMessage(content="I used the evidence already available."),
        ]
    )

    _graph, result = run_graph(python_tool, model)

    errors = error_payloads(result["messages"])
    assert [error["code"] for error in errors] == [
        "POLICY_BLOCKED",
        "POLICY_REPAIR_EXHAUSTED",
    ]
    assert errors[-1]["details"]["repair_attempts_remaining"] == 0
    assert errors[-1]["details"]["python_available"] is False
    assert python_tool.executions == [{"code": "blocked-a"}]
    assert result.get("terminal_error") is None


def test_changed_narrative_does_not_reexecute_identical_blocked_code():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "first",
                        "blocked-a",
                        analysis_goal="Original description",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "same-code",
                        "blocked-a",
                        analysis_goal="Reworded description",
                    )
                ],
            ),
            AIMessage(content="I used the available evidence."),
        ]
    )

    _graph, result = run_graph(python_tool, model)

    assert python_tool.executions == [{"code": "blocked-a"}]
    assert result.get("terminal_error") is None


def test_different_second_block_exhausts_and_third_python_call_is_rejected():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("second", "blocked-b")]),
            AIMessage(content="", tool_calls=[tool_call("third", "blocked-c")]),
            AIMessage(content="Python was unavailable, so I did not invent results."),
        ]
    )

    _graph, result = run_graph(python_tool, model)

    errors = error_payloads(result["messages"])
    assert [error["code"] for error in errors] == [
        "POLICY_BLOCKED",
        "POLICY_REPAIR_EXHAUSTED",
        "POLICY_REPAIR_EXHAUSTED",
    ]
    assert errors[1]["details"]["executed"] is False
    assert python_tool.executions == [
        {"code": "blocked-a"},
        {"code": "blocked-b"},
    ]
    assert result.get("terminal_error") is None
    assert result["final_response"].startswith("Python was unavailable")


def test_repeated_exhausted_python_does_not_disable_other_tools():
    python_tool = PolicyPythonTool()
    evidence_tool = EvidenceTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("second", "blocked-b")]),
            AIMessage(content="", tool_calls=[tool_call("again", "blocked-b")]),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("evidence", "available", name=EVIDENCE_TOOL_NAME)
                ],
            ),
            AIMessage(content="The evidence tool remained available."),
        ]
    )

    _graph, result = run_graph(
        python_tool,
        model,
        other_tools=(evidence_tool,),
    )

    assert python_tool.executions == [
        {"code": "blocked-a"},
        {"code": "blocked-b"},
    ]
    assert evidence_tool.executions == [{"code": "available"}]
    assert result.get("terminal_error") is None
    assert result["final_response"] == "The evidence tool remained available."


def test_other_tool_remains_available_after_python_policy_exhaustion():
    python_tool = PolicyPythonTool()
    evidence_tool = EvidenceTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("second", "blocked-b")]),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("evidence", "available", name=EVIDENCE_TOOL_NAME)
                ],
            ),
            AIMessage(content="The permitted evidence tool supplied the answer."),
        ]
    )

    _graph, result = run_graph(
        python_tool,
        model,
        other_tools=(evidence_tool,),
    )

    assert evidence_tool.executions == [{"code": "available"}]
    assert model.bound_schema_names[2] == (EVIDENCE_TOOL_NAME,)
    assert model.bound_schema_names[3] == (EVIDENCE_TOOL_NAME,)
    assert result["final_response"].startswith("The permitted evidence")


def test_syntax_failure_does_not_consume_python_policy_repair():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("syntax", "syntax")]),
            AIMessage(content="", tool_calls=[tool_call("blocked", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("safe", "safe")]),
            AIMessage(content="The corrected analysis completed."),
        ]
    )

    _graph, result = run_graph(python_tool, model)

    policy_error = next(
        error
        for error in error_payloads(result["messages"])
        if error["code"] == "POLICY_BLOCKED"
    )
    assert policy_error["details"]["repair_attempts_remaining"] == 1
    assert python_tool.executions == [
        {"code": "syntax"},
        {"code": "blocked-a"},
        {"code": "safe"},
    ]


def test_new_user_message_resets_python_policy_budget():
    python_tool = PolicyPythonTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first", "blocked-a")]),
            AIMessage(content="", tool_calls=[tool_call("second", "blocked-b")]),
            AIMessage(content="Python is unavailable for this user run."),
            AIMessage(content="", tool_calls=[tool_call("new-turn", "safe")]),
            AIMessage(content="The new-turn Python analysis completed."),
        ]
    )
    graph, first_result = run_graph(python_tool, model)
    follow_up = {
        "messages": follow_up_message_patch(
            list(first_result["messages"]),
            HumanMessage(content="Try a new analysis."),
        ),
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "tool_argument_repair_attempted": False,
        "current_turn_artifact_refs": [],
        "current_turn_output_artifact_refs": [],
        "completion_blocked": False,
        "model_output_state": {},
        "terminal_error": None,
        "terminal_control": None,
    }

    second_result = graph.invoke(
        follow_up,
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert model.bound_schema_names[-2] == (PYTHON_TOOL_NAME,)
    assert python_tool.executions[-1] == {"code": "safe"}
    assert second_result["final_response"] == "The new-turn Python analysis completed."

