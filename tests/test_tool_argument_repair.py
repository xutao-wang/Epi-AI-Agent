from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError
import pytest

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
    _call_model,
    _execute_tools,
    _model_answer_patch,
    _prepare_model_request,
)
from epi_agent.studies import StudyRegistry
from utils.model_runtime_profiles import (
    OPENROUTER_BASE_URL,
    PROVIDER_OPENROUTER,
    model_runtime_profile,
)


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    limit: int


class _BoundedListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    queries: list[str] = Field(max_length=5)


class _NestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[str]


class _NestedArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: _NestedPayload
    values: dict[str, int] | None = None
    numeric_values: dict[int, int] | None = None


class _SecretRejectingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    token: str

    @field_validator("token")
    @classmethod
    def reject_token(cls, value: str) -> str:
        raise PydanticCustomError(
            f"secret_{value}",
            "custom validator rejection",
        )


class _SpoofingValidatorArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    token: int

    @field_validator("token")
    @classmethod
    def reject_token(cls, value: int) -> int:
        raise PydanticCustomError(
            "greater_than",
            "custom validator rejection",
            {"gt": value},
        )


@dataclass
class _StrictTool:
    name: str = "strict_tool"
    args_model: type[BaseModel] = _StrictArguments
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="Exercise strict argument validation.",
            args_model=self.args_model,
        )

    def invoke(
        self,
        arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(message="completed")


@dataclass
class _SecretRejectingTool:
    spec: ToolSpec = ToolSpec(
        name="secret_rejecting_tool",
        description="Reject a value through a custom validator.",
        args_model=_SecretRejectingArguments,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        raise AssertionError("invalid arguments must not execute")


def _config(
    registry: ToolRegistry,
    *,
    profile=None,
) -> EpiAgentRuntimeConfig:
    studies = StudyRegistry()
    return EpiAgentRuntimeConfig(
        agent_name="test_agent",
        system_prompt="Use tools.",
        registry=registry,
        studies=studies,
        context_factory=lambda _state, _config, store: ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
        model_profile=profile or model_runtime_profile("gpt-5.4"),
    )


def _tool_call(
    arguments: dict[str, Any],
    call_id: str = "call-1",
    tool_name: str = "strict_tool",
) -> dict[str, Any]:
    return {
        "name": tool_name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }


def _execute(
    registry: ToolRegistry,
    arguments: dict[str, Any],
    *,
    repair_attempted: bool = False,
    tool_name: str = "strict_tool",
) -> dict[str, Any]:
    return _execute_tools(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[_tool_call(arguments, tool_name=tool_name)],
                )
            ],
            "artifacts": {},
            "tool_argument_repair_attempted": repair_attempted,
        },
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(registry),
    )


def test_invalid_arguments_expose_structured_issues_without_input_values() -> None:
    registry = ToolRegistry([_StrictTool()])

    with pytest.raises(ToolExecutionError) as caught:
        registry.invoke(
            "strict_tool",
            {"limit": "three"},
            context=None,  # type: ignore[arg-type]
        )

    assert caught.value.code == "INVALID_ARGUMENTS"
    assert caught.value.recoverable is True
    assert caught.value.details == {
        "issues": [
            {
                "loc": ["limit"],
                "msg": "Input does not match the tool schema.",
                "type": "int_type",
                "expected": {"type": "integer"},
            }
        ]
    }
    assert "three" not in str(caught.value.details)


def test_builtin_validation_constraint_is_safe_and_actionable() -> None:
    tool = _StrictTool(
        name="future_tool",
        args_model=_BoundedListArguments,
    )

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "future_tool",
            {"queries": ["one", "two", "three", "four", "five", "six"]},
            context=None,  # type: ignore[arg-type]
        )

    assert tool.calls == []
    assert caught.value.details == {
        "issues": [
            {
                "loc": ["queries"],
                "msg": "Input does not match the tool schema.",
                "type": "too_long",
                "expected": {
                    "maxItems": 5,
                    "type": "array",
                },
            }
        ]
    }


def test_nested_declared_field_location_remains_actionable() -> None:
    tool = _StrictTool(name="future_tool", args_model=_NestedArguments)

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "future_tool",
            {"payload": {"items": {"item": ["one"]}}},
            context=None,  # type: ignore[arg-type]
        )

    assert caught.value.details["issues"] == [
        {
            "loc": ["payload", "items"],
            "msg": "Input does not match the tool schema.",
            "type": "list_type",
            "expected": {"type": "array"},
        }
    ]


def test_object_type_error_exposes_declared_shape() -> None:
    tool = _StrictTool(name="future_tool", args_model=_NestedArguments)

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "future_tool",
            {"payload": "not-an-object"},
            context=None,  # type: ignore[arg-type]
        )

    assert caught.value.details["issues"] == [
        {
            "loc": ["payload"],
            "msg": "Input does not match the tool schema.",
            "type": "model_type",
            "expected": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {"type": "array"},
                },
            },
        }
    ]


def test_model_schema_can_inline_nested_local_references() -> None:
    schema = _StrictTool(
        name="future_tool",
        args_model=_NestedArguments,
    ).spec.model_schema(inline_local_references=True)

    parameters = schema["function"]["parameters"]
    assert "$defs" not in parameters
    assert parameters["properties"]["payload"] == {
        "additionalProperties": False,
        "properties": {
            "items": {
                "items": {"type": "string"},
                "title": "Items",
                "type": "array",
            }
        },
        "required": ["items"],
        "title": "_NestedPayload",
        "type": "object",
    }


def test_registry_inlines_references_for_every_tool_when_requested() -> None:
    registry = ToolRegistry(
        [
            _StrictTool(name="first_tool", args_model=_NestedArguments),
            _StrictTool(name="future_tool", args_model=_NestedArguments),
        ]
    )

    schemas = registry.model_schemas(inline_local_references=True)

    assert {
        schema["function"]["name"] for schema in schemas
    } == {"first_tool", "future_tool"}
    for schema in schemas:
        parameters = schema["function"]["parameters"]
        assert "$defs" not in parameters
        assert parameters["properties"]["payload"]["type"] == "object"
        assert (
            parameters["properties"]["payload"]["properties"]["items"]["type"]
            == "array"
        )


def test_openrouter_model_call_receives_inlined_tool_schemas() -> None:
    class CapturingModel:
        schemas: list[dict[str, Any]] = []

        def bind_tools(self, schemas):
            self.schemas = schemas
            return self

        def invoke(self, *_args, **_kwargs):
            return AIMessage(content="done")

    profile = replace(
        model_runtime_profile("gpt-5.4"),
        provider=PROVIDER_OPENROUTER,
        base_url=OPENROUTER_BASE_URL,
    )
    model = CapturingModel()
    registry = ToolRegistry(
        [_StrictTool(name="future_tool", args_model=_NestedArguments)]
    )

    _call_model(
        {
            "messages": [HumanMessage(content="Use the tool")],
            "artifacts": {},
            "meta": {},
        },
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(registry, profile=profile),
        model=model,
    )

    parameters = model.schemas[0]["function"]["parameters"]
    assert "$defs" not in parameters
    assert parameters["properties"]["payload"]["type"] == "object"
    assert (
        parameters["properties"]["payload"]["properties"]["items"]["type"]
        == "array"
    )


def test_non_openrouter_model_call_keeps_compact_tool_schemas() -> None:
    class CapturingModel:
        schemas: list[dict[str, Any]] = []

        def bind_tools(self, schemas):
            self.schemas = schemas
            return self

        def invoke(self, *_args, **_kwargs):
            return AIMessage(content="done")

    model = CapturingModel()
    registry = ToolRegistry(
        [_StrictTool(name="future_tool", args_model=_NestedArguments)]
    )

    _call_model(
        {
            "messages": [HumanMessage(content="Use the tool")],
            "artifacts": {},
            "meta": {},
        },
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(registry),
        model=model,
    )

    parameters = model.schemas[0]["function"]["parameters"]
    assert parameters["properties"]["payload"] == {
        "$ref": "#/$defs/_NestedPayload"
    }
    assert "$defs" in parameters


def test_dynamic_dictionary_key_is_not_exposed_in_error_location() -> None:
    sentinel = "items"
    tool = _StrictTool(name="future_tool", args_model=_NestedArguments)

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "future_tool",
            {
                "payload": {"items": []},
                "values": {sentinel: "wrong"},
            },
            context=None,  # type: ignore[arg-type]
        )

    assert sentinel not in json.dumps(caught.value.details)


def test_dynamic_integer_dictionary_key_is_not_exposed() -> None:
    sentinel = 8_675_309
    tool = _StrictTool(name="future_tool", args_model=_NestedArguments)

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "future_tool",
            {
                "payload": {"items": []},
                "numeric_values": {sentinel: "wrong"},
            },
            context=None,  # type: ignore[arg-type]
        )

    assert str(sentinel) not in json.dumps(caught.value.details)


def test_custom_validator_message_cannot_leak_rejected_value() -> None:
    sentinel = "never-expose-this-value"

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([_SecretRejectingTool()]).invoke(
            "secret_rejecting_tool",
            {"token": sentinel},
            context=None,  # type: ignore[arg-type]
        )

    assert sentinel not in str(caught.value)
    assert sentinel not in json.dumps(caught.value.details)
    assert caught.value.details["issues"][0]["type"] == "validation_error"


def test_custom_validator_cannot_spoof_builtin_error_context() -> None:
    sentinel = 8_675_309
    tool = _StrictTool(
        name="spoofing_tool",
        args_model=_SpoofingValidatorArguments,
    )

    with pytest.raises(ToolExecutionError) as caught:
        ToolRegistry([tool]).invoke(
            "spoofing_tool",
            {"token": sentinel},
            context=None,  # type: ignore[arg-type]
        )

    assert str(sentinel) not in json.dumps(caught.value.details)
    assert caught.value.details["issues"][0]["type"] == "validation_error"


def test_malformed_model_tool_call_requests_one_repair_without_replaying_it() -> None:
    malformed = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "strict_tool",
                "args": '{"limit":',
                "id": "malformed-1",
                "error": "JSON arguments are incomplete",
                "type": "invalid_tool_call",
            }
        ],
    )

    patch = _model_answer_patch(
        {"artifacts": {}, "meta": {}},
        agent_config=_config(ToolRegistry([_StrictTool()])),
        answer=malformed,
        duration_ms=1,
        iteration_count=0,
        output_state={},
        phase="idle",
    )

    assert patch["completion_blocked"] is True
    assert patch["final_response"] is None
    assert patch["tool_argument_repair_attempted"] is True
    assert len(patch["messages"]) == 1
    assert isinstance(patch["messages"][0], SystemMessage)
    feedback = json.loads(str(patch["messages"][0].content))
    assert feedback["code"] == "TOOL_ARGUMENT_REPAIR_REQUIRED"
    assert feedback["invalid_calls"] == [
        {
            "arguments": '{"limit":',
            "error": "JSON arguments are incomplete",
            "name": "strict_tool",
        }
    ]


def test_second_malformed_model_tool_call_exhausts_repair() -> None:
    malformed = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "strict_tool",
                "args": '{"limit":"still wrong"}',
                "id": "malformed-2",
                "error": "Tool arguments could not be parsed",
                "type": "invalid_tool_call",
            }
        ],
    )

    patch = _model_answer_patch(
        {
            "artifacts": {},
            "meta": {},
            "tool_argument_repair_attempted": True,
        },
        agent_config=_config(ToolRegistry([_StrictTool()])),
        answer=malformed,
        duration_ms=1,
        iteration_count=1,
        output_state={},
        phase="idle",
    )

    assert patch["terminal_error"] == {
        "code": "TOOL_ARGUMENT_REPAIR_EXHAUSTED",
        "message": (
            "The model produced malformed tool arguments after its one "
            "repair attempt."
        ),
        "recoverable": False,
    }
    assert patch["completion_blocked"] is False


def test_first_schema_invalid_call_returns_one_repair_requirement() -> None:
    tool = _StrictTool()

    patch = _execute(ToolRegistry([tool]), {"limit": "three"})

    assert tool.calls == []
    assert patch["tool_argument_repair_attempted"] is True
    error = json.loads(str(patch["messages"][0].content))["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["recoverable"] is True
    assert error["details"]["repair_required"] is True
    assert error["details"]["repair_attempts_remaining"] == 1
    assert error["details"]["instruction"] == (
        "Call this tool once more with arguments matching its advertised "
        "schema. Do not repeat the rejected arguments."
    )


def test_second_schema_invalid_call_exhausts_repair_even_with_new_arguments() -> None:
    tool = _StrictTool()

    patch = _execute(
        ToolRegistry([tool]),
        {"limit": "different invalid value"},
        repair_attempted=True,
    )

    assert tool.calls == []
    assert patch["terminal_error"] == {
        "code": "TOOL_ARGUMENT_REPAIR_EXHAUSTED",
        "message": (
            "The model produced invalid tool arguments after its one repair "
            "attempt."
        ),
        "recoverable": False,
    }
    error = json.loads(str(patch["messages"][0].content))["error"]
    assert error["code"] == "TOOL_ARGUMENT_REPAIR_EXHAUSTED"
    assert error["recoverable"] is False
    assert error["details"]["repair_attempts_remaining"] == 0


def test_schema_valid_repair_executes_and_clears_repair_state() -> None:
    tool = _StrictTool()

    patch = _execute(
        ToolRegistry([tool]),
        {"limit": 3},
        repair_attempted=True,
    )

    assert tool.calls == [{"limit": 3}]
    assert patch["messages"][0].status == "success"
    assert patch["tool_argument_repair_attempted"] is False
    assert "terminal_error" not in patch


def test_new_user_turn_starts_with_a_fresh_repair_budget() -> None:
    state = _initial_graph_state("thread-1", None)

    assert state["tool_argument_repair_attempted"] is False


def test_newly_registered_tool_automatically_inherits_repair_guardrail() -> None:
    future_tool = _StrictTool(name="future_tool")

    first = _execute(
        ToolRegistry([future_tool]),
        {"limit": "invalid"},
        tool_name="future_tool",
    )
    second = _execute(
        ToolRegistry([future_tool]),
        {"limit": "still invalid"},
        repair_attempted=first["tool_argument_repair_attempted"],
        tool_name="future_tool",
    )

    assert future_tool.calls == []
    assert json.loads(str(first["messages"][0].content))["error"]["code"] == (
        "INVALID_ARGUMENTS"
    )
    assert second["terminal_error"]["code"] == "TOOL_ARGUMENT_REPAIR_EXHAUSTED"


def test_initial_invalid_batch_gets_one_model_repair_turn() -> None:
    tool = _StrictTool()
    patch = _execute_tools(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        _tool_call({"limit": "same"}, "call-1"),
                        _tool_call({"limit": "same"}, "call-2"),
                    ],
                )
            ],
            "artifacts": {},
            "tool_argument_repair_attempted": False,
        },
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(ToolRegistry([tool])),
    )

    assert tool.calls == []
    assert "terminal_error" not in patch
    assert patch["tool_argument_repair_attempted"] is True
    assert [
        json.loads(str(message.content))["error"]["code"]
        for message in patch["messages"]
    ] == ["INVALID_ARGUMENTS", "INVALID_ARGUMENTS"]
    prepared = _prepare_model_request(
        {
            **patch,
            "iteration_count": 1,
        },
        agent_config=_config(ToolRegistry([tool])),
    )
    assert isinstance(prepared, tuple)


def test_unknown_tool_does_not_reset_pending_argument_repair() -> None:
    patch = _execute_tools(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "invented_tool",
                            "args": {},
                            "id": "unknown-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "artifacts": {},
            "tool_argument_repair_attempted": True,
        },
        {"configurable": {"thread_id": "thread-1"}},
        agent_config=_config(ToolRegistry([_StrictTool()])),
    )

    error = json.loads(str(patch["messages"][0].content))["error"]
    assert error["code"] == "UNKNOWN_TOOL"
    assert patch["tool_argument_repair_attempted"] is True
