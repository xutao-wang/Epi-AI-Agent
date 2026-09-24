from __future__ import annotations

import epi_agent.db_rag.reviews as dataset_reviews
import epi_agent.tool_packs.analysis.review as analysis_review
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from api.runtime import _initial_graph_state
from epi_agent.analysis_artifacts import AnalysisRun, save_analysis_run
from epi_agent.artifacts import DatasetPlan, StateArtifactStore
from epi_agent.db_rag.reviews import (
    RequestDatasetPlanReviewTool,
    RequestDatasetReviewTool,
)
from epi_agent.protocol import ToolContext, ToolResult, ToolSpec
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    GenericEpiAgentState,
    build_epi_agent_graph,
)
from epi_agent.studies import StudyRegistry
from epi_agent.tool_packs.analysis.review import AnalysisResultReviewTool
from utils.display_history import build_display_history
from utils.model_runtime_profiles import model_runtime_profile


def _context(store: StateArtifactStore) -> ToolContext:
    return ToolContext(
        studies=StudyRegistry(),
        artifact_store=store,
        thread_id="thread-1",
        policy=None,
    )


def _plan() -> DatasetPlan:
    return DatasetPlan.model_validate(
        {
            "study_id": "study-1",
            "goal": "Estimate a prevalence",
            "concepts": [
                {
                    "concept_id": "outcome",
                    "label": "Outcome",
                    "fields": [
                        {
                            "source": "source-1",
                            "table": "observations",
                            "column": "outcome",
                        }
                    ],
                }
            ],
        }
    )


def test_analysis_revision_returns_a_conversation_review_decision(
    monkeypatch,
) -> None:
    store = StateArtifactStore()
    reference = save_analysis_run(
        store,
        AnalysisRun(
            method="custom_python",
            specification={"code": "print(1)"},
            output_text="1\n",
            runtime={"language": "Python", "version": "3.12"},
        ),
        thread_id="thread-1",
        status="pending_review",
    )
    monkeypatch.setattr(
        analysis_review,
        "interrupt",
        lambda _payload: {
            "action": "revise",
            "feedback": "Use the requested confidence interval.",
        },
    )

    result = AnalysisResultReviewTool().invoke(
        {
            "analysis_run_id": reference.id,
            "analysis_run_version": reference.version,
        },
        _context(store),
    )

    assert getattr(result, "conversation_review_decision", None) == {
        "review_kind": "analysis_result",
        "decision": "revise",
        "text": "Use the requested confidence interval.",
    }


def test_dataset_plan_revision_returns_a_conversation_review_decision(
    monkeypatch,
) -> None:
    store = StateArtifactStore()
    reference = store.save_dataset_plan(_plan(), status="draft")
    monkeypatch.setattr(
        "epi_agent.db_rag.tools._validate_dataset_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(dataset_reviews, "_validate_plan", lambda *_args: None)
    monkeypatch.setattr(
        dataset_reviews,
        "_plan_review_view",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        dataset_reviews,
        "interrupt",
        lambda _payload: {
            "action": "revise",
            "feedback": "Use the visit-level table instead.",
        },
    )

    result = RequestDatasetPlanReviewTool().invoke(
        {"plan_id": reference.id, "version": reference.version},
        _context(store),
    )

    assert getattr(result, "conversation_review_decision", None) == {
        "review_kind": "dataset_plan",
        "decision": "revise",
        "text": "Use the visit-level table instead.",
    }


def test_dataset_revision_returns_a_conversation_review_decision(
    monkeypatch,
) -> None:
    store = StateArtifactStore()
    plan_reference = store.save_dataset_plan(_plan(), status="approved")
    dataset_reference = store.save_dataset(
        {
            "id": "dataset-1",
            "kind": "subset",
            "version": 1,
            "status": "pending_review",
            "provenance": {
                "study_id": "study-1",
                "plan_id": plan_reference.id,
                "plan_version": plan_reference.version,
                "sql_id": "sql-1",
                "sql_version": 1,
            },
        }
    )
    quality_reference = store.save_artifact(
        kind="dataset_quality_report",
        status="active",
        content={
            "dataset_id": dataset_reference.id,
            "dataset_version": dataset_reference.version,
            "plan_id": plan_reference.id,
            "plan_version": plan_reference.version,
            "sql_id": "sql-1",
            "sql_version": 1,
            "row_count": 10,
            "column_count": 1,
            "null_rates": {"outcome": 0.0},
            "duplicate_grain_rows": None,
            "grain_uniqueness": None,
            "requested_concept_coverage": {"outcome": True},
            "unexpected_columns": [],
            "join_expansion": {},
            "relationship_metrics": [],
            "warnings": [],
        },
        provenance={"study_id": "study-1"},
    )
    monkeypatch.setattr(
        dataset_reviews,
        "interrupt",
        lambda _payload: {
            "action": "revise",
            "feedback": "Exclude records without an outcome.",
        },
    )

    result = RequestDatasetReviewTool().invoke(
        {
            "dataset_id": dataset_reference.id,
            "dataset_version": dataset_reference.version,
            "quality_report_id": quality_reference.id,
            "quality_report_version": quality_reference.version,
        },
        _context(store),
    )

    assert getattr(result, "conversation_review_decision", None) == {
        "review_kind": "dataset",
        "decision": "revise",
        "text": "Exclude records without an outcome.",
    }


class _NoArguments(BaseModel):
    pass


class _ReviewFeedbackTool:
    spec = ToolSpec(
        name="test-review-feedback",
        description="Return accepted human review feedback.",
        args_model=_NoArguments,
    )

    def invoke(self, arguments, context):
        return ToolResult(
            message="Human requested a revision.",
            conversation_review_decision={
                "review_kind": "analysis_result",
                "decision": "revise",
                "text": "Use the requested confidence interval.",
            },
        )


class _ScriptedModel:
    def __init__(self) -> None:
        self.responses = iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "test-review-feedback",
                            "args": {},
                            "id": "review-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="I will revise the analysis."),
            ]
        )

    def bind_tools(self, _schemas):
        return self

    def invoke(self, _messages, **_kwargs):
        return next(self.responses)


def test_review_feedback_becomes_a_durable_user_visible_event() -> None:
    studies = StudyRegistry()
    model = _ScriptedModel()
    config = EpiAgentRuntimeConfig(
        agent_name="test",
        system_prompt="Use tools.",
        registry=ToolRegistry([_ReviewFeedbackTool()]),
        studies=studies,
        context_factory=lambda state, runnable_config, store: ToolContext(
            studies=studies,
            artifact_store=store,
            thread_id="thread-1",
            policy=None,
        ),
        model_profile=model_runtime_profile("gpt-5.4"),
    )
    graph = build_epi_agent_graph(
        state_schema=GenericEpiAgentState,
        config=config,
        model=model,
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        _initial_graph_state(
            "thread-1",
            HumanMessage(content="Analyze these data."),
        ),
        {"configurable": {"thread_id": "thread-1"}},
    )

    review_events = [
        event
        for event in result["artifacts"]["conversation_events"]
        if event["type"] == "review_decision"
    ]
    assert len(review_events) == 1
    assert review_events[0]["actor_role"] == "review"
    assert review_events[0]["review_kind"] == "analysis_result"
    assert review_events[0]["decision"] == "revise"
    assert review_events[0]["text"] == (
        "Use the requested confidence interval."
    )
    visible_feedback = [
        message
        for message in build_display_history(result)
        if isinstance(message, HumanMessage)
        and message.content == "Use the requested confidence interval."
    ]
    assert len(visible_feedback) == 1


def test_tool_result_positional_clarification_exchange_remains_compatible() -> None:
    exchange = {
        "interrupt_id": "clarify-1",
        "question": "Which population?",
        "reason": "The population is ambiguous.",
        "answer": "Adults.",
    }

    result = ToolResult("Clarified.", (), (), None, None, exchange)

    assert result.clarification_exchange == exchange
    assert result.conversation_review_decision is None
