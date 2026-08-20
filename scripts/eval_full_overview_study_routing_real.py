#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from epi_agent.agent import (
    build_epi_agent_context_prompt,
    build_general_epi_agent_registry,
    build_general_system_prompt,
)
from epi_agent.studies import StudyBundle, StudyRegistry
from epi_agent.tool_packs.studies import render_installed_study_context
from llm_vllm import build_openai_llm
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService
from utils.env_loader import load_app_environment
from utils.runtime_defaults import DEFAULT_OPENAI_MODEL


_STUDY_DEPENDENT_PREFIXES = (
    "dbrag-",
    "study-design-",
    "analysis-",
)
_STUDY_DEPENDENT_NAMES = {
    "publication-search_study_evidence",
    "publication-open_study_source",
}


class _Overview:
    def __init__(self, text: str) -> None:
        self._text = text

    def render_context(self) -> str:
        return self._text


def _study(study_id: str, label: str, scope: str) -> StudyBundle:
    overview = _Overview(f"# {label}\n\n{scope}")
    return StudyBundle(
        study_id=study_id,
        label=label,
        knowledge=None,
        catalog=None,
        data_sources={},
        study_design=overview,
        study_overview=overview,
    )


def _unavailable_study(study_id: str, label: str) -> StudyBundle:
    return StudyBundle(
        study_id=study_id,
        label=label,
        knowledge=None,
        catalog=None,
        data_sources={},
    )


SKY = _study(
    "sky-orchard",
    "Sky Orchard Cohort",
    "A cohort of cloud-orchard workers measuring participant zenthor "
    "exposure, cloud-roost sonar cadence, and aerial fruiting cycles. "
    "It contains no marine, lunar, volcanic, or crystallography data.",
)
TIDE = _study(
    "tidal-glass",
    "Tidal Glass Survey",
    "A coastal household survey measuring participant zenthor exposure, "
    "tidal-glass craft practices, and shoreline salinity. It contains no "
    "lunar, volcanic, sonar, or crystallography data.",
)


def _tool_names(record: dict[str, Any]) -> list[str]:
    return [str(call.get("name") or "") for call in record["tool_calls"]]


def _final_prose(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _assert_no_study_tool(record: dict[str, Any]) -> None:
    names = _tool_names(record)
    unexpected = [
        name
        for name in names
        if name in _STUDY_DEPENDENT_NAMES
        or name.startswith(_STUDY_DEPENDENT_PREFIXES)
    ]
    if unexpected:
        raise AssertionError(f"Unexpected study-dependent calls: {unexpected}")


def _assert_no_tool(record: dict[str, Any]) -> None:
    if record["tool_calls"]:
        raise AssertionError(
            f"Expected a prose-only response, got {record['tool_calls']!r}"
        )


def _assert_labels(record: dict[str, Any], *labels: str) -> None:
    prose = json.dumps(record["content"], ensure_ascii=False)
    missing = [label for label in labels if label not in prose]
    if missing:
        raise AssertionError(f"Response omitted live labels: {missing}")


def _assert_clarification(record: dict[str, Any]) -> None:
    if _tool_names(record) != ["general-request_clarification"]:
        raise AssertionError(
            "Ambiguous routing must call clarification alone; got "
            f"{record['tool_calls']!r}"
        )
    arguments = dict(record["tool_calls"][0].get("args") or {})
    serialized = json.dumps(arguments, ensure_ascii=False)
    for label in (SKY.label, TIDE.label):
        if label not in serialized:
            raise AssertionError(
                f"Clarification omitted live candidate {label!r}"
            )


def _assert_selected(record: dict[str, Any], study_id: str) -> None:
    calls = list(record["tool_calls"])
    if not calls:
        raise AssertionError("Scoped request did not start database retrieval.")
    first = calls[0]
    if not str(first.get("name") or "").startswith("dbrag-"):
        raise AssertionError(f"Expected DB-RAG first, got {first!r}")
    if dict(first.get("args") or {}).get("study_id") != study_id:
        raise AssertionError(
            f"Expected exact study_id {study_id!r}, got {first!r}"
        )


def _assert_unavailable(record: dict[str, Any]) -> None:
    _assert_no_study_tool(record)
    prose = json.dumps(record["content"], ensure_ascii=False).casefold()
    if "unavailable" not in prose and "configuration" not in prose:
        raise AssertionError(
            "Missing-overview response did not explain unavailable evidence."
        )


def _late_registry() -> StudyRegistry:
    studies = [
        _study(
            f"background-{index}",
            f"Background Study {index}",
            f"A survey of fictional brass-kite patterns in district {index}; "
            "it contains no opal-moss fluorescence measures.",
        )
        for index in range(1, 7)
    ]
    studies.append(
        _study(
            "opal-moss",
            "Opal Moss Registry",
            "A registry specifically measuring participant opal-moss "
            "fluorescence after nocturnal rain.",
        )
    )
    return StudyRegistry(studies)


def _cases() -> list[
    tuple[str, StudyRegistry, str, Callable[[dict[str, Any]], None]]
]:
    return [
        (
            "ambiguous_two_studies",
            StudyRegistry([SKY, TIDE]),
            "Show the distribution of participant zenthor exposure in my database.",
            _assert_clarification,
        ),
        (
            "exact_single_match",
            StudyRegistry([SKY, TIDE]),
            "Extract participant cloud-roost sonar cadence from my database.",
            lambda record: _assert_selected(record, SKY.study_id),
        ),
        (
            "incompatible_sole_study",
            StudyRegistry([SKY]),
            "Extract volcanic crystal pressure readings from my database.",
            lambda record: (
                _assert_no_tool(record),
                _assert_labels(record, SKY.label),
            ),
        ),
        (
            "explicitly_named_but_incompatible",
            StudyRegistry([SKY, TIDE]),
            "Using the Tidal Glass Survey, extract lunar crater biopsy results.",
            lambda record: (
                _assert_no_tool(record),
                _assert_labels(record, SKY.label, TIDE.label),
            ),
        ),
        (
            "general_non_database_request",
            StudyRegistry([SKY, TIDE]),
            "Explain confidence intervals generally without using a database or literature search.",
            _assert_no_study_tool,
        ),
        (
            "registration_order_forward",
            StudyRegistry([SKY, TIDE]),
            "Find cloud-roost sonar cadence in the installed data.",
            lambda record: _assert_selected(record, SKY.study_id),
        ),
        (
            "registration_order_reversed",
            StudyRegistry([TIDE, SKY]),
            "Find cloud-roost sonar cadence in the installed data.",
            lambda record: _assert_selected(record, SKY.study_id),
        ),
        (
            "applicable_study_after_first_five",
            _late_registry(),
            "Extract participant opal-moss fluorescence from my database.",
            lambda record: _assert_selected(record, "opal-moss"),
        ),
        (
            "missing_overview_fails_closed",
            StudyRegistry(
                [SKY, _unavailable_study("sealed-study", "Sealed Study")]
            ),
            "Extract participant cloud-roost sonar cadence from my database.",
            _assert_unavailable,
        ),
        (
            "empty_registry",
            StudyRegistry(),
            "Extract participant measurements from my database.",
            _assert_no_tool,
        ),
        (
            "live_labels_in_negative_response",
            StudyRegistry(
                [
                    _study(
                        "renamed-sky",
                        "Renamed Sky Package",
                        "A cloud orchard cohort with no crystallography data.",
                    ),
                    _study(
                        "new-scope",
                        "New Scope Package",
                        "A pottery survey with no crystallography data.",
                    ),
                ]
            ),
            "Extract deep-ocean crystallography values from my database.",
            lambda record: (
                _assert_no_tool(record),
                _assert_labels(
                    record,
                    "Renamed Sky Package",
                    "New Scope Package",
                ),
            ),
        ),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate real core-model full-overview study routing."
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--environment-root", type=Path, default=REPO_ROOT)
    return parser


def _evaluate(
    *,
    model: Any,
    runtime_root: Path,
    name: str,
    studies: StudyRegistry,
    query: str,
) -> dict[str, Any]:
    service = AttachmentReaderService(
        LocalAttachmentStore(runtime_root),
        runtime_root=runtime_root,
    )
    registry = build_general_epi_agent_registry(
        service=service,
        python_runtime=object(),
        runtime_root=runtime_root,
        studies=studies,
        include_db_rag=True,
    )
    context = build_epi_agent_context_prompt(
        {"artifacts": {}},
        installed_study_context=render_installed_study_context(studies),
    )
    response = model.bind_tools(registry.model_schemas()).invoke(
        [
            SystemMessage(
                content=build_general_system_prompt(
                    include_db_rag=True,
                    include_study_design=False,
                )
            ),
            HumanMessage(content=context),
            HumanMessage(content=query),
        ]
    )
    return {
        "name": name,
        "query": query,
        "installed_studies": [
            {"study_id": study.study_id, "label": study.label}
            for study in studies.values
        ],
        "content": _final_prose(response.content),
        "tool_calls": list(response.tool_calls),
        "invalid_tool_calls": list(response.invalid_tool_calls),
        "response_id": response.response_metadata.get("id"),
        "usage_metadata": response.usage_metadata,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds > 300:
        raise ValueError("The routing evaluation is limited to five minutes.")
    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="full-overview-routing-eval-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    load_app_environment(args.environment_root.expanduser().resolve())
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("A real OPENAI_API_KEY is required.")
    model_name = str(
        args.model or os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    ).strip()
    model = build_openai_llm(model_name=model_name, api_key=api_key)
    records: list[dict[str, Any]] = []
    output_path = artifact_dir / "routing-evaluation.json"
    try:
        for name, studies, query, assertion in _cases():
            if time.monotonic() >= deadline:
                raise TimeoutError("Routing evaluation exceeded its deadline.")
            record = _evaluate(
                model=model,
                runtime_root=artifact_dir / "runtime" / name,
                name=name,
                studies=studies,
                query=query,
            )
            records.append(record)
            output_path.write_text(
                json.dumps(records, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            assertion(record)
    except BaseException:
        print(f"FAIL routing evaluation; diagnostics: {artifact_dir}")
        raise

    print(
        f"PASS {len(records)}-case routing evaluation with {model_name}; "
        f"diagnostics: {artifact_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
