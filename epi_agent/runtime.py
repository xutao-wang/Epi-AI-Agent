"""Reusable bounded model/tool runtime for Epi-Agent capabilities."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, NotRequired

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableLambda
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RunnableConfig, interrupt

from epi_agent.activity import ActivitySink, NULL_ACTIVITY_SINK, notify_activity
from epi_agent.artifacts import StateArtifactStore
from epi_agent.model_responses import (
    ModelResponseProtocolError,
    observe_model_response,
)
from epi_agent.protocol import (
    ArtifactRef,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    serialize_tool_result,
)
from epi_agent.registry import ToolRegistry
from epi_agent.studies import StudyRegistry
from epi_agent.tool_call_protocol import (
    aborted_tool_messages,
    error_tool_message,
    internal_tool_error,
    tool_error_content as _tool_error_content,
)
from graph.conversation_events import (
    append_conversation_event,
    build_assistant_event,
    build_attachment_event,
    build_clarification_exchange_event,
)
from graph.state import LangChainAgentState
from graph.state import MetaKeys
from utils.model_runtime_profiles import ModelRuntimeProfile
from utils.run_cancellation import RunCancelled, cancellation_point
from utils.runtime_defaults import DEFAULT_EPI_AGENT_MAX_ITERATIONS


_REPEATED_FAILURE_LIMIT = 2
_SQL_REPAIR_ERROR_CODE = "SQL_REPAIR_REQUIRED"
_SQL_REPAIR_EXHAUSTED_CODE = "SQL_REPAIR_BUDGET_EXHAUSTED"
_SQL_REPAIR_TOOL_NAME = "dbrag-validate_and_extract"
_SQL_REPAIR_CANDIDATE_LIMIT = 5
_LOGGER = logging.getLogger(__name__)
_DATASET_ARTIFACT_KINDS = {
    "analysis_dataset",
    "dataset",
    "db_rag_result",
    "subset",
}


class GenericEpiAgentState(LangChainAgentState):
    artifact_ids: list[str]
    artifacts: dict[str, Any]
    final_response: str | None
    iteration_count: int
    failure_signatures: list[str]
    current_turn_artifact_refs: list[dict[str, Any]]
    current_turn_output_artifact_refs: list[dict[str, Any]]
    analysis_review_feedback_history: list[dict[str, Any]]
    meta: NotRequired[dict[str, Any]]
    output: NotRequired[dict[str, Any]]
    terminal_error: NotRequired[dict[str, Any]]
    terminal_control: NotRequired[dict[str, Any]]
    completion_blocked: NotRequired[bool]
    model_output_state: NotRequired[dict[str, Any]]
    cancelled_turn: NotRequired[dict[str, Any]]


ToolContextFactory = Callable[
    [dict[str, Any], RunnableConfig, StateArtifactStore], ToolContext
]
CompletionIssues = Callable[[dict[str, Any]], list[str]]
ContextPromptFactory = Callable[[dict[str, Any]], str]
ToolSuccessStateReducer = Callable[
    [dict[str, Any], str, dict[str, Any], ToolResult],
    dict[str, Any],
]


class ContextPromptError(RuntimeError):
    """Dynamic model context cannot be constructed safely."""


def _no_tool_success_state_patch(
    _state: dict[str, Any],
    _name: str,
    _arguments: dict[str, Any],
    _result: ToolResult,
) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class EpiAgentRuntimeConfig:
    agent_name: str
    system_prompt: str
    registry: ToolRegistry
    studies: StudyRegistry
    context_factory: ToolContextFactory
    model_profile: ModelRuntimeProfile
    activity_sink: ActivitySink = NULL_ACTIVITY_SINK
    completion_issues: CompletionIssues = lambda _state: []
    context_prompt_factory: ContextPromptFactory = lambda _state: ""
    tool_success_state_reducer: ToolSuccessStateReducer = (
        _no_tool_success_state_patch
    )
    max_iterations: int = DEFAULT_EPI_AGENT_MAX_ITERATIONS


def _terminal_error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "recoverable": False}


def _terminal_model_patch(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="")],
        "terminal_error": error,
        "completion_blocked": False,
    }


def _failure_signature(code: str, name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"arguments": arguments, "code": code, "tool": name},
        default=str,
        sort_keys=True,
    )


def _failure_record(signature: str) -> dict[str, Any]:
    try:
        value = json.loads(signature)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sql_repair_attempts(signatures: list[str]) -> int:
    return len(_sql_repair_failure_signatures(signatures))


def _sql_repair_failure_signatures(signatures: list[str]) -> list[str]:
    matching: list[str] = []
    for signature in signatures:
        record = _failure_record(signature)
        if (
            record.get("code") == _SQL_REPAIR_ERROR_CODE
            and record.get("tool") == _SQL_REPAIR_TOOL_NAME
        ):
            matching.append(signature)
    return matching


def _sql_repair_budget_exhausted(signatures: list[str]) -> bool:
    return _sql_repair_attempts(signatures) >= _SQL_REPAIR_CANDIDATE_LIMIT


def _repeated_failure(signatures: list[str]) -> bool:
    if signatures:
        latest = _failure_record(signatures[-1])
        if (
            latest.get("code") == _SQL_REPAIR_ERROR_CODE
            and latest.get("tool") == _SQL_REPAIR_TOOL_NAME
        ):
            return False
    return len(signatures) >= _REPEATED_FAILURE_LIMIT and all(
        item == signatures[-1] for item in signatures[-_REPEATED_FAILURE_LIMIT:]
    )


def _response_id(message: AIMessage) -> str:
    return str(
        dict(message.response_metadata or {}).get("id")
        or message.id
        or ""
    ).strip()


def _consolidate_response_chain(
    state: dict[str, Any],
    latest: AIMessage,
    response_ids: list[str],
) -> AIMessage:
    if not response_ids:
        return latest
    selected = set(response_ids)
    segments = [
        message
        for message in list(state.get("messages") or [])
        if isinstance(message, AIMessage)
        and _response_id(message) in selected
    ]
    segments.append(latest)
    text = "".join(str(message.text) for message in segments)
    tool_calls: dict[str, dict[str, Any]] = {}
    for message in segments:
        for call in list(message.tool_calls or []):
            tool_calls.setdefault(str(call["id"]), dict(call))
    return AIMessage(
        content=text,
        id=latest.id,
        response_metadata=dict(latest.response_metadata or {}),
        usage_metadata=latest.usage_metadata,
        additional_kwargs=dict(latest.additional_kwargs or {}),
        tool_calls=list(tool_calls.values()),
    )


def _record_model_observation(
    model_output_state: dict[str, Any],
    answer: AIMessage,
    *,
    duration_ms: int,
) -> tuple[dict[str, Any], Any]:
    observation = observe_model_response(answer)
    current = dict(model_output_state)
    record = {
        **observation.as_checkpoint_record(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
    }
    telemetry = [
        dict(item)
        for item in list(current.get("telemetry") or [])[-99:]
        if isinstance(item, dict)
    ]
    telemetry.append(record)
    current["telemetry"] = telemetry
    current["aggregate_input_tokens"] = sum(
        int(item.get("input_tokens") or 0) for item in telemetry
    )
    current["aggregate_output_tokens"] = sum(
        int(item.get("output_tokens") or 0) for item in telemetry
    )
    current["aggregate_reasoning_tokens"] = sum(
        int(item.get("reasoning_tokens") or 0) for item in telemetry
    )
    _LOGGER.info(
        "model_response_segment",
        extra={"model_response": record},
    )
    return current, observation


def _model_output_error_patch(
    state: dict[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    output_state = dict(state)
    output_state["phase"] = "exhausted"
    output_state["terminal_outcome"] = code
    return {
        **_terminal_model_patch(_terminal_error(code, message)),
        "model_output_state": output_state,
    }


def _insert_fresh_context(
    messages: list[Any],
    context: HumanMessage,
) -> list[Any]:
    latest_response_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, AIMessage):
            continue
        response_id = str(message.response_metadata.get("id") or "")
        if response_id.startswith("resp_"):
            latest_response_index = index
            break

    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            if index > latest_response_index:
                return [*messages[:index], context, *messages[index:]]
            break

    # Responses API compaction sends only messages after the latest resp_* AI
    # message. During a tool loop, append refreshed context after tool outputs
    # so it remains in that transmitted suffix.
    return [*messages, context]


def _adapt_messages_for_profile(
    messages: list[Any],
    profile: Any,
) -> list[Any]:
    """Providers without mid-conversation system support (Anthropic) reject
    non-consecutive system messages; re-emit them as user turns at request
    time so checkpointed state stays provider-agnostic."""
    if getattr(profile, "supports_mid_conversation_system", True):
        return messages
    # Strictest common contract (Anthropic SDK, many vLLM chat templates):
    # exactly one system message, at index 0. Later system turns are re-sent
    # as user turns; adjacent user turns merge downstream where needed.
    return [
        messages[0],
        *[
            HumanMessage(content=message.content)
            if isinstance(message, SystemMessage)
            else message
            for message in messages[1:]
        ],
    ]


def _prepare_model_request(
    state: dict[str, Any],
    *,
    agent_config: EpiAgentRuntimeConfig,
) -> (
    dict[str, Any]
    | tuple[int, dict[str, Any], str, list[Any], int]
):
    failures = list(state.get("failure_signatures") or [])
    if _repeated_failure(failures):
        return _terminal_model_patch(
            _terminal_error(
                "REPEATED_TOOL_FAILURE",
                "The same tool call failed repeatedly.",
            )
        )

    iteration_count = int(state.get("iteration_count") or 0)
    if iteration_count >= agent_config.max_iterations:
        return _terminal_model_patch(
            _terminal_error(
                "MAX_ITERATIONS",
                f"The {agent_config.agent_name} agent reached its {agent_config.max_iterations}-iteration limit.",
            )
        )

    output_state = dict(state.get("model_output_state") or {})
    phase = str(output_state.get("phase") or "idle")
    state_messages = list(state.get("messages") or [])
    try:
        context_prompt = agent_config.context_prompt_factory(state).strip()
    except ContextPromptError as error:
        return _terminal_model_patch(
            _terminal_error(
                "CONTEXT_CONFIGURATION_ERROR",
                str(error),
            )
        )
    if context_prompt:
        state_messages = _insert_fresh_context(
            state_messages,
            HumanMessage(content=context_prompt),
        )
    continuation_messages: list[SystemMessage] = []
    if phase in {"automatic", "authorized"}:
        continuation_messages.append(
            SystemMessage(
                content="Continue the same response from where it stopped."
            )
        )
    if _sql_repair_budget_exhausted(failures):
        continuation_messages.append(
            SystemMessage(
                content=json.dumps(
                    {
                        "code": _SQL_REPAIR_EXHAUSTED_CODE,
                        "instruction": (
                            "The initial SQL candidate and repairs 1 through 4 "
                            "were rejected. Do not call any tool. Explain the "
                            "final SQL diagnostic to the user, including that "
                            "no rejected SQL was executed."
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        )
    budget = (
        agent_config.model_profile.user_output_token_increment
        if phase == "authorized"
        else agent_config.model_profile.initial_output_tokens
    )
    messages = _adapt_messages_for_profile(
        [
            SystemMessage(content=agent_config.system_prompt),
            *state_messages,
            *continuation_messages,
        ],
        agent_config.model_profile,
    )
    return iteration_count, output_state, phase, messages, budget


def _activity_thread_id(config: RunnableConfig) -> str:
    configurable = dict(config.get("configurable") or {})
    return str(
        configurable.get("conversation_thread_id")
        or configurable.get("thread_id")
        or ""
    )


def _call_model(
    state: dict[str, Any],
    config: RunnableConfig,
    *,
    agent_config: EpiAgentRuntimeConfig,
    model: Any,
) -> dict[str, Any]:
    cancellation_point()
    prepared = _prepare_model_request(state, agent_config=agent_config)
    if isinstance(prepared, dict):
        return prepared
    iteration_count, output_state, phase, messages, budget = prepared
    started_at = time.perf_counter()
    cancellation_point()
    thread_id = _activity_thread_id(config)
    notify_activity(agent_config.activity_sink, "model_started", thread_id)
    try:
        answer = model.bind_tools(agent_config.registry.model_schemas()).invoke(
            messages,
            config=config,
            **agent_config.model_profile.output_budget_kwargs(budget),
        )
    finally:
        cancellation_point()
    duration_ms = max(
        0,
        int((time.perf_counter() - started_at) * 1_000),
    )
    patch = _model_answer_patch(
        state,
        agent_config=agent_config,
        answer=answer,
        duration_ms=duration_ms,
        iteration_count=iteration_count,
        output_state=output_state,
        phase=phase,
    )
    notify_activity(agent_config.activity_sink, "model_completed", thread_id)
    return patch


async def _acall_model(
    state: dict[str, Any],
    config: RunnableConfig,
    *,
    agent_config: EpiAgentRuntimeConfig,
    model: Any,
) -> dict[str, Any]:
    cancellation_point()
    prepared = _prepare_model_request(state, agent_config=agent_config)
    if isinstance(prepared, dict):
        return prepared
    iteration_count, output_state, phase, messages, budget = prepared
    started_at = time.perf_counter()
    cancellation_point()
    thread_id = _activity_thread_id(config)
    notify_activity(agent_config.activity_sink, "model_started", thread_id)
    try:
        answer = await model.bind_tools(
            agent_config.registry.model_schemas()
        ).ainvoke(
            messages,
            config=config,
            **agent_config.model_profile.output_budget_kwargs(budget),
        )
    finally:
        cancellation_point()
    duration_ms = max(
        0,
        int((time.perf_counter() - started_at) * 1_000),
    )
    patch = _model_answer_patch(
        state,
        agent_config=agent_config,
        answer=answer,
        duration_ms=duration_ms,
        iteration_count=iteration_count,
        output_state=output_state,
        phase=phase,
    )
    notify_activity(agent_config.activity_sink, "model_completed", thread_id)
    return patch


def _model_answer_patch(
    state: dict[str, Any],
    *,
    agent_config: EpiAgentRuntimeConfig,
    answer: Any,
    duration_ms: int,
    iteration_count: int,
    output_state: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    if not isinstance(answer, AIMessage):
        raise TypeError("The Epi-Agent model must return an AIMessage")

    try:
        output_state, observation = _record_model_observation(
            output_state,
            answer,
            duration_ms=duration_ms,
        )
    except ModelResponseProtocolError as exc:
        return _model_output_error_patch(
            output_state,
            code="MODEL_INCOMPLETE_RESPONSE",
            message=str(exc),
        )

    if observation.status == "incomplete":
        if observation.incomplete_reason != "max_output_tokens":
            return _model_output_error_patch(
                output_state,
                code="MODEL_INCOMPLETE_RESPONSE",
                message=(
                    "The model response was incomplete: "
                    f"{observation.incomplete_reason or 'unknown'}."
                ),
            )
        chain_response_ids = [
            str(value)
            for value in list(
                output_state.get("chain_response_ids") or []
            )
            if str(value)
        ]
        chain_response_ids.append(observation.response_id)
        output_state["chain_response_ids"] = chain_response_ids
        output_state["chain_output_tokens"] = int(
            output_state.get("chain_output_tokens") or 0
        ) + observation.output_tokens
        if phase == "authorized":
            return _model_output_error_patch(
                output_state,
                code="MODEL_OUTPUT_LIMIT_EXHAUSTED",
                message=(
                    f"{agent_config.model_profile.label} exhausted its "
                    "explicitly authorized output limit."
                ),
            )
        if phase == "automatic":
            if output_state.get("user_increment_consumed"):
                return _model_output_error_patch(
                    output_state,
                    code="MODEL_OUTPUT_LIMIT_EXHAUSTED",
                    message="The continuation increment was already used.",
                )
            output_state["phase"] = "awaiting_user"
        else:
            output_state["phase"] = "automatic"
            output_state["continuation_count"] = int(
                output_state.get("continuation_count") or 0
            ) + 1
        return {
            "messages": [answer],
            "iteration_count": iteration_count + 1,
            "completion_blocked": True,
            "final_response": None,
            "model_output_state": output_state,
        }

    answer = _consolidate_response_chain(
        state,
        answer,
        [
            str(value)
            for value in list(
                output_state.get("chain_response_ids") or []
            )
            if str(value)
        ],
    )
    output_state.update(
        {
            "phase": "idle",
            "chain_response_ids": [],
            "chain_output_tokens": 0,
            "terminal_outcome": "complete",
        }
    )

    patch: dict[str, Any] = {
        "messages": [answer],
        "iteration_count": iteration_count + 1,
        "completion_blocked": False,
        "model_output_state": output_state,
    }
    if not answer.tool_calls:
        issues = agent_config.completion_issues(
            {
                **state,
                "candidate_final_response": str(answer.text),
            }
        )
        if issues:
            patch.update(
                {
                    "messages": [
                        answer,
                        SystemMessage(
                            content=json.dumps(
                                {
                                    "code": "WORK_INCOMPLETE",
                                    "instruction": "Continue using available tools until the required work is complete.",
                                    "issues": issues,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                        ),
                    ],
                    "completion_blocked": True,
                    "final_response": None,
                }
            )
        else:
            final_response = str(answer.text)
            user_turn_hash = str(
                dict(state.get("meta") or {}).get(
                    MetaKeys.LAST_USER_MESSAGE_HASH
                )
                or ""
            ).strip() or None
            event_state = append_conversation_event(
                {
                    "artifacts": dict(state.get("artifacts") or {}),
                    "meta": dict(state.get("meta") or {}),
                },
                build_assistant_event(
                    actor=agent_config.agent_name,
                    user_turn_hash=user_turn_hash,
                    text=final_response,
                    status="done",
                ),
            )
            assistant_event_id = str(
                event_state["artifacts"]["conversation_events"][-1][
                    "event_id"
                ]
            )
            artifact_store = StateArtifactStore.from_state(event_state)
            attached_dataset_ids: set[str] = set()
            for value in list(
                state.get("current_turn_artifact_refs") or []
            ):
                if (
                    not isinstance(value, dict)
                    or value.get("kind") not in _DATASET_ARTIFACT_KINDS
                ):
                    continue
                try:
                    reference = ArtifactRef(
                        id=str(value["id"]),
                        kind=str(value["kind"]),
                        version=int(value["version"]),
                    )
                    stored = artifact_store.require(reference)
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    stored.status != "active"
                    or reference.id in attached_dataset_ids
                ):
                    continue
                event_state = append_conversation_event(
                    event_state,
                    build_attachment_event(
                        actor=agent_config.agent_name,
                        user_turn_hash=user_turn_hash,
                        artifact_id=reference.id,
                        relationship="output",
                        parent_event_id=assistant_event_id,
                        status="available",
                    ),
                )
                attached_dataset_ids.add(reference.id)
            attached_output_ids: set[str] = set()
            for value in list(
                state.get("current_turn_output_artifact_refs") or []
            ):
                if not isinstance(value, dict):
                    continue
                try:
                    reference = ArtifactRef(
                        id=str(value["id"]),
                        kind=str(value["kind"]),
                        version=int(value["version"]),
                    )
                    stored = artifact_store.require(reference)
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    reference.kind not in {"analysis_run", "figure", "table"}
                    or stored.status != "active"
                    or reference.id in attached_output_ids
                ):
                    continue
                event_state = append_conversation_event(
                    event_state,
                    build_attachment_event(
                        actor=agent_config.agent_name,
                        user_turn_hash=user_turn_hash,
                        artifact_id=reference.id,
                        relationship="output",
                        parent_event_id=assistant_event_id,
                        status="available",
                    ),
                )
                attached_output_ids.add(reference.id)
            patch.update(
                {
                    "artifacts": event_state["artifacts"],
                    "meta": event_state["meta"],
                    "final_response": final_response,
                }
            )
    return patch


def _route_model_output(state: dict[str, Any]) -> str:
    if state.get("terminal_error"):
        return "finish"
    phase = str(
        dict(state.get("model_output_state") or {}).get("phase")
        or "idle"
    )
    if phase == "awaiting_user":
        return "gate"
    if phase in {"automatic", "authorized"}:
        return "model"
    if state.get("completion_blocked"):
        return "model"
    latest = list(state.get("messages") or [])[-1]
    return "tools" if getattr(latest, "tool_calls", None) else "finish"


def _model_output_gate(
    state: dict[str, Any],
    *,
    agent_config: EpiAgentRuntimeConfig,
) -> dict[str, Any]:
    cancellation_point()
    profile = agent_config.model_profile
    incremental_cost = profile.incremental_output_cost_display
    if incremental_cost is None:
        cost_sentence = (
            "Continuing with another "
            f"{profile.user_output_token_increment:,} tokens will incur "
            "additional output charges (pricing unavailable for this model)."
        )
    else:
        cost_sentence = (
            "Continuing with another "
            f"{profile.user_output_token_increment:,} tokens may cost "
            f"up to an additional {incremental_cost} in output charges."
        )
    try:
        decision = interrupt(
            {
                "type": "model_output_limit",
                "model_id": profile.model_id,
                "model_label": profile.label,
                "automatic_token_ceiling": (
                    profile.automatic_output_token_ceiling
                ),
                "continuation_tokens": profile.user_output_token_increment,
                "additional_output_cost": incremental_cost or "unknown",
                "message": (
                    f"{profile.label} reached its "
                    f"{profile.automatic_output_token_ceiling:,}-token turn "
                    f"limit. {cost_sentence}"
                ),
                "actions": ["continue", "cancel"],
            }
        )
    finally:
        cancellation_point()
    output_state = dict(state.get("model_output_state") or {})
    if decision == {"action": "cancel"}:
        output_state.update(
            {
                "phase": "cancelled",
                "terminal_outcome": "cancelled",
            }
        )
        return {
            "completion_blocked": False,
            "final_response": None,
            "terminal_control": {
                "status": "cancelled",
                "reason": "Model continuation cancelled.",
            },
            "model_output_state": output_state,
        }
    if decision != {"action": "continue"}:
        return _model_output_error_patch(
            output_state,
            code="INVALID_MODEL_OUTPUT_DECISION",
            message="Invalid model output decision.",
        )
    if output_state.get("user_increment_consumed"):
        return _model_output_error_patch(
            output_state,
            code="MODEL_OUTPUT_LIMIT_EXHAUSTED",
            message="The continuation increment was already used.",
        )
    output_state.update(
        {
            "phase": "authorized",
            "user_increment_consumed": True,
        }
    )
    return {
        "completion_blocked": True,
        "model_output_state": output_state,
    }


def _route_model_output_gate(state: dict[str, Any]) -> str:
    if state.get("terminal_control") or state.get("terminal_error"):
        return "finish"
    return "model"


def _batch_error_messages(calls: list[dict[str, Any]]) -> tuple[list[ToolMessage], str]:
    error = ToolExecutionError(
        "INVALID_TOOL_BATCH",
        "Interrupting and state-changing tools must be called alone; only read-only tools may be batched.",
        recoverable=True,
    )
    return (
        [
            ToolMessage(
                content=_tool_error_content(error),
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
            for call in calls
        ],
        json.dumps(
            {
                "code": error.code,
                "tools": [
                    {"arguments": call["args"], "name": call["name"]}
                    for call in calls
                ],
            },
            default=str,
            sort_keys=True,
        ),
    )


def _execute_tools(
    state: dict[str, Any],
    config: RunnableConfig,
    *,
    agent_config: EpiAgentRuntimeConfig,
) -> dict[str, Any]:
    calls = list(list(state.get("messages") or [])[-1].tool_calls)
    thread_id = _activity_thread_id(config)
    artifact_store = StateArtifactStore.from_state(state)
    context = agent_config.context_factory(state, config, artifact_store)
    failures = list(state.get("failure_signatures") or [])
    feedback_history = [
        dict(entry)
        for entry in list(
            state.get("analysis_review_feedback_history") or []
        )[-20:]
        if isinstance(entry, dict)
    ]
    if len(calls) > 1:
        specs = []
        for call in calls:
            try:
                specs.append(agent_config.registry.spec(call["name"]))
            except ToolExecutionError:
                continue
        if any(spec.interrupting or not spec.read_only for spec in specs):
            messages, signature = _batch_error_messages(calls)
            return {
                "messages": messages,
                "artifacts": artifact_store.snapshot(),
                "failure_signatures": [*failures, signature],
            }

    artifact_ids = list(state.get("artifact_ids") or [])
    known_ids = set(artifact_ids)
    refs = list(state.get("current_turn_artifact_refs") or [])
    output_refs = list(state.get("current_turn_output_artifact_refs") or [])
    known_refs = {
        (str(item.get("id") or ""), str(item.get("kind") or ""), item.get("version"))
        for item in refs
        if isinstance(item, dict)
    }
    known_output_refs = {
        (str(item.get("id") or ""), str(item.get("kind") or ""), item.get("version"))
        for item in output_refs
        if isinstance(item, dict)
    }
    messages: list[ToolMessage] = []
    terminal_control: dict[str, Any] | None = None
    terminal_error: dict[str, Any] | None = None
    clarification_exchanges: list[dict[str, str]] = []
    tool_state_patch: dict[str, Any] = {}
    for call_index, call in enumerate(calls):
        cancellation_point()
        name = call["name"]
        arguments = call["args"]
        activity_started = False
        if (
            name == _SQL_REPAIR_TOOL_NAME
            and _sql_repair_budget_exhausted(failures)
        ):
            error = ToolExecutionError(
                _SQL_REPAIR_EXHAUSTED_CODE,
                (
                    "The initial SQL candidate and repairs 1 through 4 were "
                    "already rejected; a sixth candidate was not executed."
                ),
                recoverable=False,
                details={
                    "max_candidate_attempts": _SQL_REPAIR_CANDIDATE_LIMIT,
                    "candidate_attempt": _SQL_REPAIR_CANDIDATE_LIMIT + 1,
                    "repairs_remaining": 0,
                    "executed": False,
                },
            )
            messages.append(error_tool_message(call, error))
            messages.extend(aborted_tool_messages(calls[call_index + 1 :]))
            terminal_error = _terminal_error(error.code, str(error))
            break
        try:
            cancellation_point()
            agent_config.registry.spec(name)
            activity_started = True
            notify_activity(
                agent_config.activity_sink,
                "tool_started",
                thread_id,
                call["id"],
                name,
            )
            try:
                result = agent_config.registry.invoke(
                    name,
                    arguments,
                    context=context,
                )
            finally:
                cancellation_point()
        except (GraphInterrupt, RunCancelled):
            raise
        except ToolExecutionError as error:
            if (
                name == _SQL_REPAIR_TOOL_NAME
                and error.code == _SQL_REPAIR_ERROR_CODE
            ):
                candidate_attempt = _sql_repair_attempts(failures) + 1
                details = dict(error.details or {})
                details.update(
                    {
                        "candidate_attempt": candidate_attempt,
                        "max_candidate_attempts": _SQL_REPAIR_CANDIDATE_LIMIT,
                        "repairs_remaining": max(
                            0,
                            _SQL_REPAIR_CANDIDATE_LIMIT - candidate_attempt,
                        ),
                        "executed": False,
                    }
                )
                error = ToolExecutionError(
                    error.code,
                    str(error),
                    recoverable=error.recoverable,
                    details=details,
                )
            messages.append(error_tool_message(call, error))
            failures.append(_failure_signature(error.code, name, arguments))
            if not error.recoverable:
                messages.extend(
                    aborted_tool_messages(calls[call_index + 1 :])
                )
                terminal_error = _terminal_error(error.code, str(error))
                break
            if activity_started:
                notify_activity(
                    agent_config.activity_sink,
                    "tool_recoverable_failure",
                    thread_id,
                    call["id"],
                )
            continue
        except Exception:
            _LOGGER.exception(
                "Unexpected registered tool failure",
                extra={
                    "tool_name": name,
                    "tool_call_id": str(call["id"]),
                    "thread_id": thread_id,
                },
            )
            error = internal_tool_error()
            messages.append(error_tool_message(call, error))
            messages.extend(aborted_tool_messages(calls[call_index + 1 :]))
            failures.append(_failure_signature(error.code, name, arguments))
            terminal_error = _terminal_error(error.code, str(error))
            break

        notify_activity(
            agent_config.activity_sink,
            "tool_completed",
            thread_id,
            call["id"],
            name,
        )
        failures = (
            []
            if name == _SQL_REPAIR_TOOL_NAME
            else _sql_repair_failure_signatures(failures)
        )
        reducer_state = {**state, **tool_state_patch}
        tool_state_patch.update(
            agent_config.tool_success_state_reducer(
                reducer_state,
                name,
                arguments,
                result,
            )
        )
        messages.append(
            ToolMessage(
                content=serialize_tool_result(result),
                artifact=result.artifacts,
                tool_call_id=call["id"],
                name=name,
            )
        )
        for reference in result.artifacts:
            reference_key = (reference.id, reference.kind, reference.version)
            if reference_key not in known_refs:
                refs.append(
                    {
                        "id": reference.id,
                        "kind": reference.kind,
                        "version": reference.version,
                    }
                )
                known_refs.add(reference_key)
            if reference.id not in known_ids:
                artifact_ids.append(reference.id)
                known_ids.add(reference.id)
        for reference in result.output_artifacts:
            reference_key = (reference.id, reference.kind, reference.version)
            if reference_key not in known_output_refs:
                output_refs.append(
                    {
                        "id": reference.id,
                        "kind": reference.kind,
                        "version": reference.version,
                    }
                )
                known_output_refs.add(reference_key)
        if result.review_feedback_entry is not None:
            feedback_history.append(dict(result.review_feedback_entry))
            feedback_history = feedback_history[-20:]
        if result.clarification_exchange is not None:
            clarification_exchanges.append(
                dict(result.clarification_exchange)
            )
        pending_analysis_references: list[ArtifactRef] = []
        for reference in result.artifacts:
            if reference.kind != "analysis_run":
                continue
            try:
                stored = artifact_store.require(reference)
            except (KeyError, TypeError, ValueError):
                continue
            if stored.status == "pending_review":
                pending_analysis_references.append(reference)
        if (
            pending_analysis_references
            and feedback_history
            and feedback_history[-1].get("action") == "revise"
            and feedback_history[-1].get("replacement_analysis_run")
            is None
        ):
            replacement = pending_analysis_references[-1]
            feedback_history[-1] = {
                **feedback_history[-1],
                "replacement_analysis_run": {
                    "id": replacement.id,
                    "kind": replacement.kind,
                    "version": replacement.version,
                },
            }
        if result.terminal_control is not None:
            terminal_control = {
                "status": result.terminal_control.status,
                "reason": result.terminal_control.reason,
            }

    event_state = {
        "artifacts": artifact_store.snapshot(),
        "meta": dict(state.get("meta") or {}),
    }
    user_turn_hash = str(
        event_state["meta"].get(MetaKeys.LAST_USER_MESSAGE_HASH) or ""
    ).strip() or None
    for exchange in clarification_exchanges:
        event_state = append_conversation_event(
            event_state,
            build_clarification_exchange_event(
                actor=agent_config.agent_name,
                user_turn_hash=user_turn_hash,
                interrupt_id=str(exchange["interrupt_id"]),
                question=str(exchange["question"]),
                reason=str(exchange["reason"]),
                answer=str(exchange["answer"]),
            ),
        )
    if terminal_control is not None and terminal_control.get("status") == "cancelled":
        event_state = _append_cancelled_dataset_event(
            event_state,
            agent_name=agent_config.agent_name,
            user_turn_hash=user_turn_hash,
            artifact_store=artifact_store,
            artifact_refs=refs,
        )

    patch: dict[str, Any] = {
        "messages": messages,
        "artifact_ids": artifact_ids,
        "current_turn_artifact_refs": refs,
        "current_turn_output_artifact_refs": output_refs,
        "artifacts": event_state["artifacts"],
        "meta": event_state["meta"],
        "failure_signatures": failures,
        "analysis_review_feedback_history": feedback_history,
        **tool_state_patch,
    }
    if terminal_control is not None:
        patch.update(
            {
                "terminal_control": terminal_control,
            }
        )
    if terminal_error is not None:
        patch.update(
            {
                "terminal_error": terminal_error,
                "completion_blocked": False,
            }
        )
    return patch


def analysis_completion_issues(state: dict[str, Any]) -> list[str]:
    if _sql_repair_budget_exhausted(
        list(state.get("failure_signatures") or [])
    ):
        return []
    store = StateArtifactStore.from_state(state)
    current_refs = list(state.get("current_turn_artifact_refs") or [])
    latest_plan: ArtifactRef | None = None
    for value in current_refs:
        if not isinstance(value, dict) or value.get("kind") != "dataset_plan":
            continue
        try:
            latest_plan = ArtifactRef(
                id=str(value["id"]),
                kind="dataset_plan",
                version=int(value["version"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    if latest_plan is not None:
        try:
            stored_plan = store.require(latest_plan)
        except (KeyError, ValueError):
            return [
                f"dataset_plan_stale:{latest_plan.id}:v{latest_plan.version}"
            ]
        if stored_plan.status == "draft":
            if list(stored_plan.content.get("unresolved") or []):
                return [
                    "dataset_plan_unresolved_requires_general_request_clarification:"
                    f"{latest_plan.id}:v{latest_plan.version}"
                ]
            return [
                f"dataset_plan_review_required:{latest_plan.id}:"
                f"v{latest_plan.version}"
            ]
        if stored_plan.status == "approved":
            latest_dataset: tuple[ArtifactRef, Any] | None = None
            for value in current_refs:
                if (
                    not isinstance(value, dict)
                    or value.get("kind") not in _DATASET_ARTIFACT_KINDS
                ):
                    continue
                try:
                    reference = ArtifactRef(
                        id=str(value["id"]),
                        kind=str(value["kind"]),
                        version=int(value["version"]),
                    )
                    dataset = store.require(reference)
                except (KeyError, TypeError, ValueError):
                    continue
                provenance = {
                    **dict(dataset.provenance or {}),
                    **dict(dataset.content.get("provenance") or {}),
                }
                if (
                    provenance.get("plan_id") == latest_plan.id
                    and provenance.get("plan_version")
                    == latest_plan.version
                ):
                    latest_dataset = (reference, dataset)
            if latest_dataset is None:
                return [
                    f"dataset_extraction_required:{latest_plan.id}:"
                    f"v{latest_plan.version}"
                ]

            dataset_ref, dataset = latest_dataset
            if dataset.status == "active":
                pass
            elif dataset.status == "pending_review":
                inspected = False
                for value in current_refs:
                    if (
                        not isinstance(value, dict)
                        or value.get("kind") != "dataset_quality_report"
                    ):
                        continue
                    try:
                        quality = store.require(
                            ArtifactRef(
                                id=str(value["id"]),
                                kind="dataset_quality_report",
                                version=int(value["version"]),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    quality_dataset = dict(
                        quality.provenance.get("dataset") or {}
                    )
                    if (
                        (
                            quality.content.get("dataset_id")
                            == dataset_ref.id
                            and quality.content.get("dataset_version")
                            == dataset_ref.version
                        )
                        or (
                            quality_dataset.get("id") == dataset_ref.id
                            and quality_dataset.get("version")
                            == dataset_ref.version
                        )
                    ):
                        inspected = True
                        break
                if not inspected:
                    return [
                        f"dataset_inspection_required:{dataset_ref.id}:"
                        f"v{dataset_ref.version}"
                    ]
                return [
                    f"dataset_review_required:{dataset_ref.id}:"
                    f"v{dataset_ref.version}"
                ]
            else:
                return [
                    f"dataset_extraction_required:{latest_plan.id}:"
                    f"v{latest_plan.version}"
                ]

    latest: ArtifactRef | None = None
    for value in current_refs:
        if not isinstance(value, dict) or value.get("kind") != "analysis_run":
            continue
        try:
            latest = ArtifactRef(
                id=str(value["id"]),
                kind="analysis_run",
                version=int(value["version"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    if latest is None:
        return []
    try:
        status = store.require(latest).status
    except (KeyError, ValueError):
        return [
            f"analysis_result_stale:{latest.id}:v{latest.version}"
        ]
    if status == "pending_review":
        return [
            f"analysis_result_pending_review:{latest.id}:v{latest.version}"
        ]
    if status == "rejected":
        candidate = str(
            state.get("candidate_final_response") or ""
        ).strip()
        if candidate.endswith("?") and candidate.count("?") == 1:
            return []
        return [
            f"analysis_revision_required:{latest.id}:v{latest.version}"
        ]
    return []


def _route_tools_output(state: dict[str, Any]) -> str:
    return "finish" if state.get("terminal_control") or state.get("terminal_error") else "model"


def _append_cancelled_dataset_event(
    event_state: dict[str, Any],
    *,
    agent_name: str,
    user_turn_hash: str | None,
    artifact_store: StateArtifactStore,
    artifact_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    dataset_refs: list[ArtifactRef] = []
    seen_ids: set[str] = set()
    for value in artifact_refs:
        if (
            not isinstance(value, dict)
            or value.get("kind") not in _DATASET_ARTIFACT_KINDS
        ):
            continue
        try:
            reference = ArtifactRef(
                id=str(value["id"]),
                kind=str(value["kind"]),
                version=int(value["version"]),
            )
            stored = artifact_store.require(reference)
        except (KeyError, TypeError, ValueError):
            continue
        if stored.status != "active" or reference.id in seen_ids:
            continue
        dataset_refs.append(reference)
        seen_ids.add(reference.id)
    if not dataset_refs:
        return event_state

    event_state = append_conversation_event(
        event_state,
        build_assistant_event(
            actor=agent_name,
            user_turn_hash=user_turn_hash,
            text=(
                "The analysis workflow was cancelled. The approved dataset "
                "and its SQL provenance remain available below; Python "
                "analysis outputs were not published."
            ),
            status="cancelled",
        ),
    )
    assistant_event_id = str(
        event_state["artifacts"]["conversation_events"][-1]["event_id"]
    )
    for reference in dataset_refs:
        event_state = append_conversation_event(
            event_state,
            build_attachment_event(
                actor=agent_name,
                user_turn_hash=user_turn_hash,
                artifact_id=reference.id,
                relationship="output",
                parent_event_id=assistant_event_id,
                status="available",
            ),
        )
    return event_state


def build_epi_agent_graph(
    *,
    state_schema: type[Any],
    config: EpiAgentRuntimeConfig,
    model: Any | None = None,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    if config.max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    if model is None:
        raise ValueError("model is required")
    workflow = StateGraph(state_schema)
    model_node = RunnableLambda(
        partial(_call_model, agent_config=config, model=model),
        afunc=partial(_acall_model, agent_config=config, model=model),
    )
    workflow.add_node(
        "model",
        model_node,
    )
    workflow.add_node(
        "model_output_gate",
        partial(_model_output_gate, agent_config=config),
    )
    workflow.add_node("tools", partial(_execute_tools, agent_config=config))
    workflow.add_edge(START, "model")
    workflow.add_conditional_edges(
        "model",
        _route_model_output,
        {
            "model": "model",
            "gate": "model_output_gate",
            "tools": "tools",
            "finish": END,
        },
    )
    workflow.add_conditional_edges(
        "model_output_gate",
        _route_model_output_gate,
        {"model": "model", "finish": END},
    )
    workflow.add_conditional_edges("tools", _route_tools_output, {"model": "model", "finish": END})
    return workflow.compile(checkpointer=checkpointer).with_config(
        {"recursion_limit": (2 * config.max_iterations) + 8}
    )


__all__ = [
    "ContextPromptError",
    "EpiAgentRuntimeConfig",
    "GenericEpiAgentState",
    "analysis_completion_issues",
    "build_epi_agent_graph",
]
