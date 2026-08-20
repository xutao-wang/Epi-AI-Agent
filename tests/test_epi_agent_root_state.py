from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel

from epi_agent.agent import (
    GENERAL_CORE_INSTRUCTIONS,
    EpiAgentState,
    build_epi_agent_context_prompt,
)
from epi_agent.protocol import ToolContext, ToolResult, ToolSpec
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    ContextPromptError,
    EpiAgentRuntimeConfig,
    GenericEpiAgentState,
    build_epi_agent_graph,
)
import epi_agent.runtime as runtime_module
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.studies import render_installed_study_context
from graph.state import MetaKeys
from utils.model_runtime_profiles import model_runtime_profile
from llm_vllm import build_openai_llm


class EmptyArguments(BaseModel):
    pass


def _studies() -> StudyRegistry:
    return StudyRegistry(
        [
            StudyBundle(
                study_id="study-1",
                label="Study",
                knowledge=object(),
                catalog=object(),
                data_sources={},
            )
        ]
    )


class FinalModel:
    def __init__(self) -> None:
        self.observed_messages: list[Any] = []

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> "FinalModel":
        return self

    def invoke(
        self,
        messages: list[Any],
        *,
        config: dict[str, Any],
        **_kwargs: Any,
    ) -> AIMessage:
        del config
        self.observed_messages = list(messages)
        return AIMessage(content="Final epidemiology answer.")


def _runtime(
    *,
    model: Any,
    registry: ToolRegistry | None = None,
    context_prompt_factory: Any | None = None,
) -> Any:
    studies = _studies()
    return build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        model=model,
        config=EpiAgentRuntimeConfig(
                model_profile=model_runtime_profile("gpt-5.4"),
            agent_name="epi_agent",
            system_prompt="Core instructions.",
            registry=registry or ToolRegistry(),
            studies=studies,
            context_factory=lambda state, _config, artifact_store: ToolContext(
                studies=studies,
                artifact_store=artifact_store,
                thread_id="thread-1",
                policy=None,
            ),
            context_prompt_factory=(
                context_prompt_factory
                or (lambda _state: "Authorized artifact cards: []")
            ),
        ),
    )


def _state() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="Answer this question.")],
        "active_study_id": "study-1",
        "artifact_ids": [],
        "artifacts": {},
        "meta": {MetaKeys.LAST_USER_MESSAGE_HASH: "turn-1"},
        "output": {},
        "final_response": None,
        "iteration_count": 0,
        "failure_signatures": [],
        "current_turn_artifact_refs": [],
        "analysis_review_feedback_history": [],
    }


def test_root_runtime_records_final_answer_in_same_conversation_store() -> None:
    model = FinalModel()

    result = _runtime(model=model).invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    events = result["artifacts"]["conversation_events"]
    assert events[-1]["type"] == "assistant"
    assert events[-1]["actor"] == "epi_agent"
    assert events[-1]["user_turn_hash"] == "turn-1"
    assert events[-1]["text"] == "Final epidemiology answer."
    assert result["final_response"] == "Final epidemiology answer."
    assert isinstance(model.observed_messages[0], SystemMessage)
    assert isinstance(model.observed_messages[1], HumanMessage)
    assert model.observed_messages[1].content == "Authorized artifact cards: []"


def test_context_configuration_error_stops_before_the_model_call() -> None:
    model = FinalModel()

    def broken_context(_state: dict[str, Any]) -> str:
        raise ContextPromptError("Installed-study routing context is too large.")

    result = _runtime(
        model=model,
        context_prompt_factory=broken_context,
    ).invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert result["terminal_error"] == {
        "code": "CONTEXT_CONFIGURATION_ERROR",
        "message": "Installed-study routing context is too large.",
        "recoverable": False,
    }
    assert model.observed_messages == []


def test_refreshed_context_survives_responses_api_history_compaction() -> None:
    studies = _studies()
    state = _state()
    state["messages"] = [
        HumanMessage(content="Original database question."),
        AIMessage(
            content="",
            response_metadata={"id": "resp_previous"},
            tool_calls=[
                {
                    "name": "db-tool",
                    "args": {},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="tool output", tool_call_id="call-1"),
    ]
    agent_config = EpiAgentRuntimeConfig(
        model_profile=model_runtime_profile("gpt-5.4"),
        agent_name="epi_agent",
        system_prompt="Core instructions.",
        registry=ToolRegistry(),
        studies=studies,
        context_factory=lambda state, _config, artifact_store: ToolContext(
            studies=studies,
            artifact_store=artifact_store,
            thread_id="thread-1",
            policy=None,
        ),
        context_prompt_factory=lambda _state: "fresh routing marker",
    )

    prepared = runtime_module._prepare_model_request(
        state,
        agent_config=agent_config,
    )

    assert not isinstance(prepared, dict)
    request_messages = prepared[3]
    payload = build_openai_llm(
        model_name="gpt-5.4",
        api_key="test-key",
    )._get_request_payload(request_messages)
    assert payload["previous_response_id"] == "resp_previous"
    assert "fresh routing marker" in json.dumps(payload["input"])


@dataclass(frozen=True)
class ClarificationExchangeTool:
    spec = ToolSpec(
        name="general-request_clarification",
        description="Return one clarification answer.",
        args_model=EmptyArguments,
    )

    def invoke(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        return ToolResult(
            message="Human clarification answer: 12 months",
            clarification_exchange={
                "interrupt_id": "interrupt-1",
                "question": "Which recurrence window should be used?",
                "reason": "The outcome definition is ambiguous.",
                "answer": "12 months",
            },
        )


class ClarificationModel:
    def __init__(self) -> None:
        self.step = 0

    def bind_tools(self, _schemas: list[dict[str, Any]]) -> "ClarificationModel":
        return self

    def invoke(
        self,
        _messages: list[Any],
        *,
        config: dict[str, Any],
        **_kwargs: Any,
    ) -> AIMessage:
        del config
        self.step += 1
        if self.step == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "general-request_clarification",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Analysis can now proceed.")


def test_root_runtime_records_clarification_exchange_without_handoff() -> None:
    result = _runtime(
        model=ClarificationModel(),
        registry=ToolRegistry([ClarificationExchangeTool()]),
    ).invoke(
        _state(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    exchange = next(
        event
        for event in result["artifacts"]["conversation_events"]
        if event["type"] == "clarification_exchange"
    )
    assert exchange == {
        **exchange,
        "actor": "epi_agent",
        "user_turn_hash": "turn-1",
        "interrupt_id": "interrupt-1",
        "question": "Which recurrence window should be used?",
        "reason": "The outcome definition is ambiguous.",
        "answer": "12 months",
    }
    assert "orchestrator" not in result
    assert "epi_agent_handoff" not in result.get("meta", {})


def test_epi_context_prompt_contains_bounded_cards_without_rows_or_paths() -> None:
    prompt = build_epi_agent_context_prompt(
        {
            "messages": [
                HumanMessage(
                    content="Analyze the attached follow-up data.",
                    additional_kwargs={
                        "attachment_ids": ["attachment-followup"],
                    },
                )
            ],
            "authorized_attachment_ids": [
                "attachment-1",
                "attachment-followup",
            ],
            "current_turn_artifact_refs": [
                {
                    "id": "subset-agent-1",
                    "kind": "subset",
                    "version": 1,
                }
            ],
            "artifacts": {
                "attachments": {
                    "attachment-1": {
                        "id": "attachment-1",
                        "kind": "csv",
                        "version": 1,
                        "status": "available",
                        "filename": "cohort.csv",
                        "created_at": "2026-07-31T22:00:00+00:00",
                        "inspection": {
                            "columns": ["sex", "tb_status"],
                            "row_count": 1000,
                        },
                        "path": "/private/data/cohort.csv",
                    },
                    "attachment-2": {
                        "id": "attachment-2",
                        "kind": "csv",
                        "version": 1,
                        "status": "available",
                        "filename": "not-authorized.csv",
                    },
                    "attachment-followup": {
                        "id": "attachment-followup",
                        "kind": "csv",
                        "version": 1,
                        "status": "available",
                        "filename": "followup.csv",
                        "created_at": "2026-08-01T00:00:00+00:00",
                    },
                },
                "datasets": {
                    "uploaded-1": {
                        "id": "uploaded-1",
                        "kind": "uploaded",
                        "version": 1,
                        "status": "active",
                        "columns": ["person_id", "recurrence"],
                        "created_at": "2026-07-31T23:00:00+00:00",
                        "rows": [{"person_id": "secret"}],
                        "path": "/private/data/dataset.csv",
                        "provenance": {
                            "source": "message_attachment",
                            "source_attachment_ids": [
                                "attachment-followup",
                            ],
                        },
                    },
                    "subset-agent-1": {
                        "id": "subset-agent-1",
                        "kind": "subset",
                        "version": 1,
                        "status": "active",
                        "columns": ["subject_id", "outcome"],
                        "created_at": "2026-07-31T21:00:00+00:00",
                        "provenance": {
                            "producer": "dbrag-validate_and_extract",
                            "plan_id": "plan-1",
                            "plan_version": 1,
                            "sql_id": "sql-1",
                            "sql_version": 1,
                            "source_question": (
                                "Create the approved TB cohort."
                            ),
                            "source_tables": ["screening", "follow_up"],
                        },
                    },
                },
                "conversation_events": [
                    {
                        "event_id": "message-cohort",
                        "type": "user",
                        "text": "Upload the cohort.",
                        "created_at": "2026-07-31T22:01:00+00:00",
                    },
                    {
                        "event_id": "event-attachment-cohort",
                        "type": "attachment",
                        "artifact_id": "attachment-1",
                        "relationship": "input",
                        "parent_event_id": "message-cohort",
                    },
                    {
                        "event_id": "message-followup",
                        "type": "user",
                        "text": "Analyze the attached follow-up data.",
                        "created_at": "2026-08-01T00:01:00+00:00",
                    },
                    {
                        "event_id": "event-attachment-followup",
                        "type": "attachment",
                        "artifact_id": "attachment-followup",
                        "relationship": "input",
                        "parent_event_id": "message-followup",
                    },
                ],
            },
        }
    )
    cards = json.loads(prompt.split("\n", 1)[1])
    attachment_card = next(card for card in cards if card["id"] == "attachment-1")
    current_attachment_card = next(
        card for card in cards if card["id"] == "attachment-followup"
    )
    uploaded_card = next(card for card in cards if card["id"] == "uploaded-1")
    db_rag_card = next(
        card for card in cards if card["id"] == "subset-agent-1"
    )

    assert "attachment-1" in prompt
    assert "attachment-2" not in prompt
    assert attachment_card["origin_message_id"] == "message-cohort"
    assert attachment_card["created_at"] == "2026-07-31T22:00:00+00:00"
    assert attachment_card["origin_message_created_at"] == (
        "2026-07-31T22:01:00+00:00"
    )
    assert attachment_card["current_turn"] is False
    assert attachment_card["inspection"] == {
        "columns": ["sex", "tb_status"],
        "row_count": 1000,
    }
    assert current_attachment_card["created_at"] == (
        "2026-08-01T00:00:00+00:00"
    )
    assert current_attachment_card["origin_message_created_at"] == (
        "2026-08-01T00:01:00+00:00"
    )
    assert current_attachment_card["current_turn"] is True
    assert uploaded_card["created_at"] == "2026-07-31T23:00:00+00:00"
    assert uploaded_card["source_type"] == "message_attachment"
    assert uploaded_card["source_filenames"] == ["followup.csv"]
    assert uploaded_card["origin_message_ids"] == ["message-followup"]
    assert uploaded_card["current_turn"] is True
    assert db_rag_card["source_type"] == "dbrag-validate_and_extract"
    assert db_rag_card["created_at"] == "2026-07-31T21:00:00+00:00"
    assert db_rag_card["plan_id"] == "plan-1"
    assert db_rag_card["sql_id"] == "sql-1"
    assert db_rag_card["source_question"] == (
        "Create the approved TB cohort."
    )
    assert db_rag_card["current_turn"] is True
    assert "person_id" in prompt
    assert "secret" not in prompt
    assert "/private/data" not in prompt
    assert '"rows"' not in prompt


def test_epi_context_prompt_omits_missing_card_timestamps() -> None:
    prompt = build_epi_agent_context_prompt(
        {
            "messages": [HumanMessage(content="Use the earlier file.")],
            "authorized_attachment_ids": ["attachment-legacy"],
            "current_turn_artifact_refs": [],
            "artifacts": {
                "attachments": {
                    "attachment-legacy": {
                        "id": "attachment-legacy",
                        "kind": "tabular",
                        "version": 1,
                        "status": "available",
                        "filename": "legacy.csv",
                        "created_at": 123,
                    }
                },
                "datasets": {
                    "dataset-legacy": {
                        "id": "dataset-legacy",
                        "kind": "uploaded",
                        "version": 1,
                        "status": "active",
                        "columns": ["sex", "tb_status"],
                        "created_at": ["invalid"],
                        "provenance": {},
                    }
                },
                "conversation_events": [
                    {
                        "event_id": "message-legacy",
                        "type": "user",
                        "text": "Upload legacy data.",
                        "created_at": {"invalid": True},
                    },
                    {
                        "event_id": "attachment-event-legacy",
                        "type": "attachment",
                        "artifact_id": "attachment-legacy",
                        "relationship": "input",
                        "parent_event_id": "message-legacy",
                    },
                ],
            },
        }
    )

    cards = json.loads(prompt.split("\n", 1)[1])
    attachment_card = next(
        card for card in cards if card["id"] == "attachment-legacy"
    )
    dataset_card = next(
        card for card in cards if card["id"] == "dataset-legacy"
    )
    assert attachment_card["current_turn"] is False
    assert "created_at" not in attachment_card
    assert "origin_message_created_at" not in attachment_card
    assert "created_at" not in dataset_card


def test_epi_context_prompt_includes_installed_study_context() -> None:
    study_context = (
        '{"context_kind":"installed_study_routing_evidence",'
        '"study_count":0,"studies":[]}'
    )
    prompt = build_epi_agent_context_prompt(
        {"messages": []},
        installed_study_context=study_context,
    )

    assert study_context in prompt


def test_study_context_is_sorted_and_reports_unavailable_overviews() -> None:
    study_context = render_installed_study_context(
        StudyRegistry(
            [
                StudyBundle("z", "Zulu", None, None, {}),
                StudyBundle("a", "Alpha", None, None, {}),
            ]
        )
    )

    payload = json.loads(study_context)
    assert [study["study_id"] for study in payload["studies"]] == ["a", "z"]
    assert all(
        study["overview_available"] is False
        for study in payload["studies"]
    )


def test_general_prompt_uses_runtime_owned_dataset_binding() -> None:
    assert "`dataset`" in GENERAL_CORE_INSTRUCTIONS
    assert "already-loaded pandas.DataFrame" in GENERAL_CORE_INSTRUCTIONS
    assert "Never load attachment files from generated Python" in (
        GENERAL_CORE_INSTRUCTIONS
    )
    assert "Select datasets by exact ID, kind, and version." in (
        GENERAL_CORE_INSTRUCTIONS
    )
    assert "Prefer a current-turn artifact only when its schema" in (
        GENERAL_CORE_INSTRUCTIONS
    )
    assert "Use creation time only as a tie-breaker" in (
        GENERAL_CORE_INSTRUCTIONS
    )
    assert "Never select an artifact solely because it is newest" in (
        GENERAL_CORE_INSTRUCTIONS
    )
    assert "datasets[selected_dataset_id]" not in GENERAL_CORE_INSTRUCTIONS


def test_root_state_schema_has_no_orchestrator_handoff_contract() -> None:
    annotations = EpiAgentState.__annotations__

    assert "meta" in annotations
    assert "output" in annotations
    assert "acceptance_checks" not in annotations
    assert "authorized_dataset_ids" not in annotations
