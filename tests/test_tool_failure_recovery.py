from __future__ import annotations

import json
from dataclasses import replace
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
from epi_agent.runtimes.python.models import PythonRuntimeFailure
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.python.tools import _runtime_failure
from utils.model_runtime_profiles import model_runtime_profile


class Arguments(BaseModel):
    code: str


class FailingTool:
    spec = ToolSpec(
        name="analysis-test",
        description="Exercise shared tool-failure recovery.",
        args_model=Arguments,
    )

    def __init__(self) -> None:
        self.executions: list[dict[str, str]] = []

    def invoke(self, arguments, context):
        self.executions.append(arguments)
        raise ToolExecutionError(
            "EXECUTION_FAILED",
            "Column 'age' was not found in the approved dataset.",
            recoverable=True,
            details={"category": "generated_code"},
        )


class CorrectableTool(FailingTool):
    def invoke(self, arguments, context):
        self.executions.append(arguments)
        if arguments["code"] == "corrected":
            return ToolResult(message="Corrected analysis completed.")
        raise ToolExecutionError(
            "EXECUTION_FAILED",
            "Column 'age' was not found in the approved dataset.",
            recoverable=True,
            details={"category": "generated_code"},
        )


class MissingDependencyTool(FailingTool):
    def invoke(self, arguments, context):
        self.executions.append(arguments)
        raise ToolExecutionError(
            "DEPENDENCY_NOT_AVAILABLE",
            "Package “sklearn” isn’t installed.",
            recoverable=False,
            details={
                "category": "unsupported_runtime",
                "missing_module": "sklearn",
            },
        )


class SuccessfulTool:
    spec = ToolSpec(
        name="analysis-other",
        description="Exercise a different successful tool call.",
        args_model=Arguments,
    )

    def __init__(self) -> None:
        self.executions: list[dict[str, str]] = []

    def invoke(self, arguments, context):
        self.executions.append(arguments)
        return ToolResult(message="Other tool completed.")


class AnyArguments(BaseModel):
    value: Any


class AnyFailingTool:
    spec = ToolSpec(
        name="any-analysis",
        description="Exercise type-sensitive argument comparison.",
        args_model=AnyArguments,
    )

    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []

    def invoke(self, arguments, context):
        self.executions.append(arguments)
        raise ToolExecutionError(
            "EXECUTION_FAILED",
            "Generated analysis failed.",
            recoverable=True,
        )


class ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = iter(responses)
        self.bound_schema_counts: list[int] = []
        self.message_batches: list[list[object]] = []

    def bind_tools(self, schemas):
        self.bound_schema_counts.append(len(schemas))
        return self

    def invoke(self, messages, **kwargs):
        self.message_batches.append(list(messages))
        return next(self.responses)


def tool_call(
    call_id: str,
    code: object = "bad",
    *,
    name: str = "analysis-test",
    argument_name: str = "code",
) -> dict[str, object]:
    return {
        "name": name,
        "args": {argument_name: code},
        "id": call_id,
        "type": "tool_call",
    }


def configuration(tool, *other_tools) -> EpiAgentRuntimeConfig:
    studies = StudyRegistry()
    return EpiAgentRuntimeConfig(
        agent_name="test",
        system_prompt="Use tools.",
        registry=ToolRegistry([tool, *other_tools]),
        studies=studies,
        context_factory=lambda state, config, store: ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )


def run_graph(
    tool,
    model: ScriptedModel,
    *,
    completion_issues=None,
    max_iterations: int | None = None,
    other_tools=(),
):
    config = configuration(tool, *other_tools)
    if completion_issues is not None:
        config = replace(config, completion_issues=completion_issues)
    if max_iterations is not None:
        config = replace(config, max_iterations=max_iterations)
    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        config=config,
        model=model,
        checkpointer=InMemorySaver(),
    )
    state = _initial_graph_state(
        "thread-1",
        HumanMessage(content="Analyze the approved dataset."),
    )
    return graph.invoke(state, {"configurable": {"thread_id": "thread-1"}})


def test_first_recoverable_failure_gives_the_model_actionable_shared_guidance():
    tool = CorrectableTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first")]),
            AIMessage(content="", tool_calls=[tool_call("corrected", "corrected")]),
            AIMessage(content="The corrected analysis completed."),
        ]
    )

    result = run_graph(tool, model)

    first_error = next(
        message
        for message in model.message_batches[1]
        if isinstance(message, ToolMessage) and message.status == "error"
    )
    payload = json.loads(first_error.content)
    assert payload["error"] == {
        "code": "EXECUTION_FAILED",
        "details": {
            "category": "generated_code",
            "instruction": (
                "Correct the call, choose another tool, or answer from existing "
                "evidence. Do not repeat the failed call unchanged."
            ),
        },
        "message": "Column 'age' was not found in the approved dataset.",
        "recoverable": True,
    }
    assert tool.executions == [{"code": "bad"}, {"code": "corrected"}]
    assert result["final_response"] == "The corrected analysis completed."


def test_unchanged_failed_call_is_not_executed_twice_and_recovers_as_text():
    tool = FailingTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("first")]),
            AIMessage(content="", tool_calls=[tool_call("duplicate")]),
            AIMessage(
                content=(
                    "I could not calculate the requested estimate, but the approved "
                    "dataset remains available for a revised analysis."
                )
            ),
        ]
    )

    result = run_graph(
        tool,
        model,
        completion_issues=lambda _state: ["analysis_result_missing"],
    )

    assert tool.executions == [{"code": "bad"}]
    assert model.bound_schema_counts == [1, 1]
    assert result.get("terminal_error") is None
    assert result["final_response"].startswith("I could not calculate")
    assert any(
        "do not call any tool" in str(message.content).casefold()
        and "do not invent unavailable results" in str(message.content).casefold()
        for message in model.message_batches[2]
    )


def test_unchanged_failed_call_is_not_executed_twice_within_one_batch():
    tool = FailingTool()
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[tool_call("first"), tool_call("duplicate")],
            ),
            AIMessage(content="The analysis could not be completed."),
        ]
    )

    result = run_graph(tool, model)

    assert tool.executions == [{"code": "bad"}]
    assert model.bound_schema_counts == [1]
    assert result.get("terminal_error") is None
    assert result["final_response"] == "The analysis could not be completed."


def test_duplicate_invalid_arguments_keep_the_schema_repair_path():
    tool = FailingTool()
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("first", {"invalid": "value"}),
                    tool_call("duplicate", {"invalid": "value"}),
                ],
            ),
            AIMessage(content="The tool arguments could not be repaired."),
        ]
    )

    result = run_graph(tool, model)

    assert tool.executions == []
    assert [
        json.loads(str(message.content))["error"]["code"]
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ] == ["INVALID_ARGUMENTS", "INVALID_ARGUMENTS"]
    assert result["final_response"] == "The tool arguments could not be repaired."


def test_python_runtime_failure_is_not_double_encoded():
    error = _runtime_failure(
        PythonRuntimeFailure(
            "EXECUTION_FAILED",
            "Column 'age' was not found.",
            category="retryable_code",
            recoverable=True,
        )
    )

    assert str(error) == "Column 'age' was not found."
    assert error.details == {
        "category": "retryable_code",
        "execution_started": False,
    }


def test_missing_dependency_ends_only_the_current_agent_run():
    tool = MissingDependencyTool()
    model = ScriptedModel(
        [AIMessage(content="", tool_calls=[tool_call("missing")])]
    )

    result = run_graph(tool, model)

    assert tool.executions == [{"code": "bad"}]
    assert result["terminal_error"] == {
        "code": "DEPENDENCY_NOT_AVAILABLE",
        "message": "Package “sklearn” isn’t installed.",
        "recoverable": False,
    }


def test_failure_in_read_only_batch_remains_protected_after_another_tool_succeeds():
    failing = FailingTool()
    successful = SuccessfulTool()
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("failed"),
                    tool_call("other", "ok", name="analysis-other"),
                ],
            ),
            AIMessage(content="", tool_calls=[tool_call("duplicate")]),
            AIMessage(content="The available evidence is insufficient."),
        ]
    )

    result = run_graph(failing, model, other_tools=(successful,))

    assert failing.executions == [{"code": "bad"}]
    assert successful.executions == [{"code": "ok"}]
    assert model.bound_schema_counts == [2, 2]
    assert result["final_response"] == "The available evidence is insufficient."


def test_other_read_only_calls_run_even_when_batch_contains_a_rejected_duplicate():
    failing = FailingTool()
    successful = SuccessfulTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("failed")]),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("duplicate"),
                    tool_call("other", "ok", name="analysis-other"),
                ],
            ),
            AIMessage(content="The other tool provided the available evidence."),
        ]
    )

    result = run_graph(failing, model, other_tools=(successful,))

    assert failing.executions == [{"code": "bad"}]
    assert successful.executions == [{"code": "ok"}]
    assert result["final_response"].startswith("The other tool")


def test_later_sibling_failure_does_not_mask_duplicate_recovery():
    first = FailingTool()
    second = FailingTool()
    second.spec = replace(second.spec, name="analysis-second")
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("failed")]),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("duplicate"),
                    tool_call("second", name="analysis-second"),
                ],
            ),
            AIMessage(content="Neither analysis produced a valid result."),
        ]
    )

    result = run_graph(first, model, other_tools=(second,))

    assert first.executions == [{"code": "bad"}]
    assert second.executions == [{"code": "bad"}]
    assert model.bound_schema_counts == [2, 2]
    assert result["final_response"].startswith("Neither analysis")


def test_each_failure_in_a_read_only_batch_is_protected_from_reexecution():
    first = FailingTool()
    second = FailingTool()
    second.spec = replace(second.spec, name="analysis-second")
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("first"),
                    tool_call("second", name="analysis-second"),
                ],
            ),
            AIMessage(content="", tool_calls=[tool_call("duplicate-first")]),
            AIMessage(content="Neither analysis produced a valid result."),
        ]
    )

    result = run_graph(first, model, other_tools=(second,))

    assert first.executions == [{"code": "bad"}]
    assert second.executions == [{"code": "bad"}]
    assert result["final_response"].startswith("Neither analysis")


def test_changed_json_scalar_type_is_not_treated_as_an_identical_call():
    tool = AnyFailingTool()
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "boolean",
                        True,
                        name="any-analysis",
                        argument_name="value",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "integer",
                        1,
                        name="any-analysis",
                        argument_name="value",
                    )
                ],
            ),
            AIMessage(content="No valid analysis result is available."),
        ]
    )

    result = run_graph(tool, model)

    assert tool.executions == [{"value": True}, {"value": 1}]
    assert result["final_response"] == "No valid analysis result is available."


def test_final_recovery_turn_is_reserved_beyond_normal_iteration_limit():
    tool = FailingTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("failed")]),
            AIMessage(content="", tool_calls=[tool_call("duplicate")]),
            AIMessage(content="The analysis could not be completed."),
        ]
    )

    result = run_graph(tool, model, max_iterations=2)

    assert result.get("terminal_error") is None
    assert result["final_response"] == "The analysis could not be completed."


def test_incomplete_final_recovery_is_not_invoked_again():
    tool = FailingTool()
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("failed")]),
            AIMessage(content="", tool_calls=[tool_call("duplicate")]),
            AIMessage(
                content="The analysis could not be completed within the available run.",
                id="resp_recovery",
                response_metadata={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            ),
            AIMessage(content="Unexpected second recovery invocation."),
        ]
    )

    result = run_graph(tool, model)

    assert len(model.message_batches) == 3
    assert result["final_response"].startswith("The analysis could not be completed")
