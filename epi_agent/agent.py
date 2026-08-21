"""General single-loop EpiAgent configuration."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RunnableConfig
from langchain_core.messages import HumanMessage

from epi_agent.activity import ActivitySink, NULL_ACTIVITY_SINK
from epi_agent.artifacts import StateArtifactStore
from epi_agent.attachments.tools import build_attachment_tool_registry
from epi_agent.db_rag.prompt import DB_RAG_SYSTEM_PROMPT
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext
from epi_agent.registry import ToolRegistry
from epi_agent.runtime import (
    EpiAgentRuntimeConfig,
    GenericEpiAgentState,
    analysis_completion_issues,
    build_epi_agent_graph,
)
from epi_agent.runtimes.python import LocalPythonRuntime
from epi_agent.studies import SearchableStudyDesignProvider, StudyRegistry
from epi_agent.tool_packs.analysis import (
    build_analysis_review_tool_registry,
)
from epi_agent.tool_packs.analysis.python import (
    build_custom_python_tool_registry,
)
from epi_agent.tool_packs.general import build_general_tool_registry
from epi_agent.tool_packs.publication import (
    build_publication_system_prompt,
    build_publication_tool_registry,
)
from epi_agent.tool_packs.publication.pubmed import is_pubmed_configured
from epi_agent.tool_packs.study_design import (
    STUDY_DESIGN_SYSTEM_PROMPT,
    build_study_design_tool_registry,
)
from epi_agent.tool_packs.studies import (
    STUDY_ROUTING_SYSTEM_PROMPT,
    render_installed_study_context,
)
from utils.attachment_readers import AttachmentReaderService
from utils.dataset_artifacts import is_selectable_dataset_artifact
from utils.model_runtime_profiles import ModelRuntimeProfile
from utils.runtime_defaults import DEFAULT_EPI_AGENT_MAX_ITERATIONS
from utils.user_storage import UserStorageLayout


_MAX_CARD_COLUMNS = 100
_MAX_CARD_VALUE_CHARS = 500
_MAX_CARD_LIST_ITEMS = 20


GENERAL_CORE_INSTRUCTIONS = """You are the single reasoning owner for
epidemiology requests. Keep the user's exact scientific goal in view across
inspection, evidence retrieval, method selection, analysis, and final
interpretation. Use the registered capabilities iteratively in this same
conversation. Do not hand results to another agent for interpretation.
Choose the least sufficient action; database extraction is not required for
literature, metadata, or general epidemiology questions.

Clarification rules:
- Before asking any clarification, use applicable registered evidence tools
  when they can establish the answer.
- Ask when human intent or knowledge is genuinely required. Never guess merely
  to avoid clarification.
- If investigation demonstrates a limitation that user input cannot resolve,
  report the limitation.
- Do not repeat a clarification the user has answered or that subsequent
  evidence has resolved.

Attachment rules:
- Use only authorized attachment IDs. Inspect before selecting a reader.
- Load a table when an attachment must become an analysis dataset.
- Never treat an attachment ID as a dataset ID.
- Select datasets by exact ID, kind, and version. Use explicit user IDs or
  filenames first, then current-turn provenance, then an exact dataset just
  produced or approved by the current workflow.
- Use the only selectable dataset when exactly one exists; never silently join
  separate tables.
- Prefer a current-turn artifact only when its schema and provenance can answer
  the request. Compare relevance before recency.
- Use creation time only as a tie-breaker between otherwise suitable candidates.
- Never select an artifact solely because it is newest.
- Inspect uploaded table candidates before analysis. Choose `current_upload` only
  when its schema can answer the request; choose `prior_artifact` only when the
  upload is unsuitable. Ask for clarification when multiple candidates fit.

Analysis rules:
- Inspect a dataset before fitting when its row grain, coding, or missingness
  is not already established.
- Never silently deduplicate repeated observations or pretend that adding time
  as a covariate resolves within-participant correlation.
- For repeated observations, obtain an exact analysis timepoint or a
  longitudinal method decision before writing custom Python.
- Python is the only available analysis runtime. Never offer R code or describe R as an available runtime.
- Custom Python runs without a code-review pause in the bounded local runtime.
  Never install packages at runtime.
- Custom Python receives the one exact selected input as the already-loaded pandas.DataFrame
  `dataset`; analyze `dataset` directly.
- Never load attachment files from generated Python, discover file paths, or
  select a different dataset inside generated code.
- Generated Python must print the requested human-readable tables,
  statistical estimates, uncertainty, sample sizes, events, diagnostics,
  assumptions, and limitations.
- Use ordinary Matplotlib calls for requested figures. The runtime captures
  the latest open figure; do not save files.
- Do not assign scientific results to a special JSON variable. Printed text
  and the captured figure are the reviewable outputs.
- Every custom-Python analysis result is staged pending final review. After an
  analysis tool returns an analysis_run,
  call analysis-request_result_review alone with its exact ID and version.
- Result approval permits publication but does not force interpretation or
  workflow completion. Interpret only when the user requested interpretation.
- On result revision feedback, stay in this same reasoning loop: revise and
  rerun the analysis, or obtain a statistical clarification when the requested
  change requires a user choice. Never route the feedback to another agent.
- When a scientific or workflow decision has two or more plausible choices,
  call general-request_clarification alone. Supply one concise question, its
  reason, and two or more concise evidence-supported concrete options. Never
  include a variant of "let the agent decide" or "you choose" as an option:
  the UI supplies that one standard delegation choice. Never ask this question
  in ordinary assistant text. This applies to ambiguous
  attachments, datasets, repeated-record reductions, and analysis methods.
  If the user delegates the decision, choose exactly one supplied option from
  the available evidence and continue without reopening the clarification.
- A post-review clarification must be one concise question ending in exactly
  one question mark; do not include numerical claims from the rejected result.
- Never report numerical analysis results from a pending, rejected, or
  cancelled analysis_run.
- Report the requested numerical estimates, uncertainty, sample size, events,
  diagnostics, warnings, assumptions, and limitations directly in the final
  answer.

General utility rules:
- Use weather, web search, or calculation only when it materially helps the
  current request.
- Treat web results as external evidence and identify their source in the
  final answer.
- Do not use a general utility as a substitute for study evidence, database
  inspection, or epidemiological analysis."""


def build_general_system_prompt(
    *,
    include_db_rag: bool,
    include_study_design: bool = False,
) -> str:
    sections = [
        GENERAL_CORE_INSTRUCTIONS,
        STUDY_ROUTING_SYSTEM_PROMPT,
        build_publication_system_prompt(
            include_pubmed=is_pubmed_configured(),
        ),
    ]
    if include_db_rag:
        sections.append(DB_RAG_SYSTEM_PROMPT)
    if include_study_design:
        sections.append(STUDY_DESIGN_SYSTEM_PROMPT)
    return "\n\n".join(sections)


GENERAL_SYSTEM_PROMPT = build_general_system_prompt(include_db_rag=True)


class EpiAgentState(GenericEpiAgentState):
    meta: dict[str, Any]
    output: dict[str, Any]
    authorized_attachment_ids: list[str]


def epi_agent_completion_issues(state: dict[str, Any]) -> list[str]:
    return analysis_completion_issues(state)


def build_epi_agent_context_prompt(
    state: dict[str, Any],
    *,
    installed_study_context: str = "",
) -> str:
    artifacts = dict(state.get("artifacts") or {})
    authorized_attachment_ids = set(
        _unique_strings(state.get("authorized_attachment_ids"))
    )
    attachments = dict(artifacts.get("attachments") or {})
    datasets = dict(artifacts.get("datasets") or {})
    current_attachment_ids = _latest_human_attachment_ids(state)
    current_turn_dataset_ids = {
        str(reference.get("id") or "")
        for reference in list(state.get("current_turn_artifact_refs") or [])
        if isinstance(reference, dict)
    }
    attachment_origins = _attachment_origin_message_ids(artifacts)
    event_created_at_by_id = _conversation_event_created_at_by_id(artifacts)
    cards: list[dict[str, Any]] = []

    for attachment_id in sorted(authorized_attachment_ids):
        attachment = attachments.get(attachment_id)
        if not isinstance(attachment, dict):
            continue
        inspection = dict(attachment.get("inspection") or {})
        inspection_card: dict[str, Any] = {}
        columns = inspection.get("columns")
        if isinstance(columns, list):
            inspection_card["columns"] = [
                _bounded_text(column)
                for column in columns[:_MAX_CARD_COLUMNS]
            ]
        row_count = inspection.get("row_count")
        if isinstance(row_count, int) and not isinstance(row_count, bool):
            inspection_card["row_count"] = row_count
        origins = attachment_origins.get(attachment_id) or []
        origin_message_id = origins[0] if origins else ""
        card = {
            "id": attachment_id,
            "kind": _bounded_text(attachment.get("kind") or "attachment"),
            "version": int(attachment.get("version") or 1),
            "status": _bounded_text(attachment.get("status")),
            "filename": _bounded_text(attachment.get("filename")),
            "created_at": _bounded_timestamp(attachment.get("created_at")),
            "origin_message_id": origin_message_id or None,
            "origin_message_created_at": event_created_at_by_id.get(
                origin_message_id
            ),
            "current_turn": attachment_id in current_attachment_ids,
            "inspection": inspection_card or None,
        }
        cards.append(
            {
                key: value
                for key, value in card.items()
                if value is not None and value != "" and value != [] and value != {}
            }
        )

    for dataset_id, raw_dataset in sorted(datasets.items()):
        if not isinstance(raw_dataset, dict):
            continue
        dataset = dict(raw_dataset)
        if not is_selectable_dataset_artifact(dataset):
            continue
        columns = [
            _bounded_text(column)
            for column in list(dataset.get("columns") or [])[:_MAX_CARD_COLUMNS]
        ]
        provenance = dict(dataset.get("provenance") or {})
        source_attachment_ids = _bounded_strings(
            provenance.get("source_attachment_ids")
        )
        source_filenames = _bounded_strings(
            provenance.get("source_filenames")
        )
        source_filenames = _bounded_strings(
            [
                *source_filenames,
                *[
                    str(
                        dict(attachments.get(attachment_id) or {}).get(
                            "filename"
                        )
                        or ""
                    )
                    for attachment_id in source_attachment_ids
                ],
            ]
        )
        origin_message_ids = _bounded_strings(
            [
                message_id
                for attachment_id in source_attachment_ids
                for message_id in attachment_origins.get(attachment_id, [])
            ]
        )
        card = {
            "id": str(dataset_id),
            "kind": _bounded_text(dataset.get("kind") or "dataset"),
            "version": int(dataset.get("version") or 1),
            "status": _bounded_text(dataset.get("status")),
            "created_at": _bounded_timestamp(dataset.get("created_at")),
            "columns": columns,
            "columns_truncated": len(list(dataset.get("columns") or []))
            > len(columns),
            "source_type": _bounded_text(
                provenance.get("source") or provenance.get("producer")
            ),
            "source_attachment_ids": source_attachment_ids,
            "source_filenames": source_filenames,
            "origin_message_ids": origin_message_ids,
            "current_turn": bool(
                set(source_attachment_ids) & current_attachment_ids
                or str(dataset_id) in current_turn_dataset_ids
            ),
            "plan_id": _bounded_text(provenance.get("plan_id")),
            "plan_version": _positive_int(provenance.get("plan_version")),
            "sql_id": _bounded_text(provenance.get("sql_id")),
            "sql_version": _positive_int(provenance.get("sql_version")),
            "source_question": _bounded_text(
                provenance.get("source_question")
            ),
            "source_tables": _bounded_strings(
                provenance.get("source_tables")
            ),
        }
        cards.append(
            {
                key: value
                for key, value in card.items()
                if value is not None
                and value != ""
                and value != []
                and value != {}
            }
        )

    artifact_context = (
        "Current authoritative artifact snapshot for this conversation. "
        "This complete inventory supersedes all earlier artifact snapshots; "
        "use current_turn and status values only from this snapshot. These "
        "cards contain metadata only; use tools to inspect contents:\n"
        + json.dumps(
            cards,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    contexts = [artifact_context]
    cancelled_turn = dict(state.get("cancelled_turn") or {})
    cancelled_text = _bounded_text(cancelled_turn.get("text"))
    cancelled_attachment_ids = _bounded_strings(
        cancelled_turn.get("attachment_ids")
    )
    if cancelled_text or cancelled_attachment_ids:
        cancelled_context = {
            "text": cancelled_text,
            "attachment_ids": cancelled_attachment_ids,
        }
        contexts.append(
            "Most recent cancelled user turn. This record is inactive: do not "
            "continue it unless the latest user message explicitly asks to retry, "
            "continue, or refer to that work. Restart tools from the latest approved "
            "state; never claim to resume a partial tool execution. If a referenced "
            "attachment cannot be inspected, ask the user to reattach it:\n"
            + json.dumps(
                cancelled_context,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    study_context = str(installed_study_context or "").strip()
    if study_context:
        contexts.append(study_context)
    return "\n\n".join(contexts)


def _unique_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )


def _bounded_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _MAX_CARD_VALUE_CHARS:
        return text
    return text[: _MAX_CARD_VALUE_CHARS - 3] + "..."


def _bounded_timestamp(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _bounded_text(value)


def _bounded_strings(value: object) -> list[str]:
    return [
        bounded
        for item in _unique_strings(value)[:_MAX_CARD_LIST_ITEMS]
        if (bounded := _bounded_text(item))
    ]


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _latest_human_attachment_ids(state: dict[str, Any]) -> set[str]:
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, HumanMessage):
            continue
        additional_kwargs = dict(message.additional_kwargs or {})
        return set(_bounded_strings(additional_kwargs.get("attachment_ids")))
    return set()


def _attachment_origin_message_ids(
    artifacts: dict[str, Any],
) -> dict[str, list[str]]:
    origins: dict[str, list[str]] = {}
    for event in list(artifacts.get("conversation_events") or []):
        if (
            not isinstance(event, dict)
            or event.get("type") != "attachment"
            or event.get("relationship") != "input"
        ):
            continue
        attachment_id = _bounded_text(event.get("artifact_id"))
        parent_event_id = _bounded_text(event.get("parent_event_id"))
        if not attachment_id or not parent_event_id:
            continue
        values = origins.setdefault(attachment_id, [])
        if (
            parent_event_id not in values
            and len(values) < _MAX_CARD_LIST_ITEMS
        ):
            values.append(parent_event_id)
    return origins


def _conversation_event_created_at_by_id(
    artifacts: dict[str, Any],
) -> dict[str, str]:
    timestamps: dict[str, str] = {}
    for event in list(artifacts.get("conversation_events") or []):
        if not isinstance(event, dict):
            continue
        event_id = _bounded_text(event.get("event_id"))
        created_at = _bounded_timestamp(event.get("created_at"))
        if event_id and created_at:
            timestamps[event_id] = created_at
    return timestamps


def build_general_epi_agent_graph(
    *,
    llm: Any,
    model_profile: ModelRuntimeProfile,
    service: AttachmentReaderService,
    studies: StudyRegistry,
    runtime_root: str | Path | None,
    python_runtime: Any | None = None,
    include_db_rag: bool = True,
    checkpointer: Any | None = None,
    max_iterations: int = DEFAULT_EPI_AGENT_MAX_ITERATIONS,
    activity_sink: ActivitySink = NULL_ACTIVITY_SINK,
) -> CompiledStateGraph:
    include_study_design = any(
        isinstance(study.study_design, SearchableStudyDesignProvider)
        for study in studies.values
    )
    registry = build_general_epi_agent_registry(
        service=service,
        python_runtime=python_runtime,
        runtime_root=runtime_root,
        include_db_rag=include_db_rag,
        studies=studies,
    )

    def context_factory(
        state: dict[str, Any],
        config: RunnableConfig,
        artifact_store: StateArtifactStore,
    ) -> ToolContext:
        configurable = dict(config.get("configurable") or {})
        owner_user_id = str(
            configurable.get("owner_user_id") or "local-user"
        ).strip()
        conversation_thread_id = str(
            configurable.get("conversation_thread_id")
            or configurable.get("thread_id")
            or ""
        ).strip()
        return ToolContext(
            studies=studies,
            artifact_store=artifact_store,
            thread_id=conversation_thread_id,
            policy=None,
            thread_storage=(
                UserStorageLayout(runtime_root).thread(
                    owner_user_id,
                    conversation_thread_id,
                )
                if runtime_root is not None and owner_user_id and conversation_thread_id
                else None
            ),
            attachment_store=service.store,
            authorized_attachment_ids=tuple(
                state.get("authorized_attachment_ids") or []
            ),
            current_attachment_ids=tuple(
                sorted(_latest_human_attachment_ids(state))
            ),
            analysis_review_feedback_history=tuple(
                dict(entry)
                for entry in list(
                    state.get("analysis_review_feedback_history") or []
                )
                if isinstance(entry, dict)
            ),
        )

    def context_prompt_factory(state: dict[str, Any]) -> str:
        return build_epi_agent_context_prompt(
            state,
            installed_study_context=render_installed_study_context(
                studies,
                max_chars=model_profile.routing_context_char_ceiling,
            ),
        )

    return build_epi_agent_graph(
        state_schema=EpiAgentState,
        model=llm,
        config=EpiAgentRuntimeConfig(
            agent_name="epi_agent",
            system_prompt=build_general_system_prompt(
                include_db_rag=include_db_rag,
                include_study_design=include_study_design,
            ),
            registry=registry,
            studies=studies,
            context_factory=context_factory,
            activity_sink=activity_sink,
            completion_issues=epi_agent_completion_issues,
            context_prompt_factory=context_prompt_factory,
            model_profile=model_profile,
            max_iterations=max_iterations,
        ),
        checkpointer=checkpointer,
    )


def build_general_epi_agent_registry(
    *,
    service: AttachmentReaderService,
    python_runtime: Any | None = None,
    runtime_root: str | Path | None,
    include_db_rag: bool = True,
    studies: StudyRegistry | None = None,
) -> ToolRegistry:
    resolved_python_runtime = python_runtime or LocalPythonRuntime(
        runtime_root=runtime_root,
    )
    return ToolRegistry(
        [
            *build_general_tool_registry().tools(),
            *build_attachment_tool_registry(service).tools(),
            *build_publication_tool_registry().tools(),
            *(
                build_study_design_tool_registry().tools()
                if studies is not None
                and any(
                    isinstance(
                        study.study_design,
                        SearchableStudyDesignProvider,
                    )
                    for study in studies.values
                )
                else ()
            ),
            *(
                build_db_rag_tool_registry().tools()
                if include_db_rag
                else ()
            ),
            *build_custom_python_tool_registry(
                resolved_python_runtime,
                runtime_root=runtime_root,
            ).tools(),
            *build_analysis_review_tool_registry().tools(),
        ]
    )


__all__ = [
    "EpiAgentState",
    "GENERAL_CORE_INSTRUCTIONS",
    "GENERAL_SYSTEM_PROMPT",
    "build_general_system_prompt",
    "epi_agent_completion_issues",
    "build_epi_agent_context_prompt",
    "build_general_epi_agent_graph",
    "build_general_epi_agent_registry",
]
