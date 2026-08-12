from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ToolActivityLabels:
    started: str
    waiting: str | None = None
    completed: str | None = None


_LABELS = {
    "attachments-inspect": ToolActivityLabels("Inspecting an attachment"),
    "attachments-inspect_image": ToolActivityLabels("Inspecting an image"),
    "attachments-load_table": ToolActivityLabels("Loading a data table"),
    "attachments-parse_structured": ToolActivityLabels("Reading structured data"),
    "attachments-read_document": ToolActivityLabels("Reading a document"),
    "analysis-run_custom_python": ToolActivityLabels("Running statistical analysis"),
    "analysis-request_result_review": ToolActivityLabels(
        "Preparing analysis review",
        "Waiting for analysis review",
        "Analysis reviewed",
    ),
    "dbrag-open_artifact": ToolActivityLabels("Opening database evidence"),
    "dbrag-search_catalog": ToolActivityLabels("Searching the data catalog"),
    "dbrag-inspect_table": ToolActivityLabels("Inspecting a database table"),
    "dbrag-find_join_paths": ToolActivityLabels(
        "Finding relationships between tables"
    ),
    "dbrag-profile_relationship": ToolActivityLabels(
        "Checking table relationships"
    ),
    "dbrag-save_dataset_plan": ToolActivityLabels("Saving the dataset plan"),
    "dbrag-validate_dataset_plan": ToolActivityLabels(
        "Validating the dataset plan"
    ),
    "dbrag-validate_and_extract": ToolActivityLabels("Creating the dataset"),
    "dbrag-inspect_dataset": ToolActivityLabels("Checking dataset quality"),
    "dbrag-request_dataset_plan_review": ToolActivityLabels(
        "Preparing dataset plan review",
        "Waiting for dataset plan review",
        "Dataset plan reviewed",
    ),
    "dbrag-request_dataset_review": ToolActivityLabels(
        "Preparing dataset review",
        "Waiting for dataset approval",
        "Dataset reviewed",
    ),
    "general-calculate": ToolActivityLabels("Calculating a result"),
    "general-get_weather_tips": ToolActivityLabels("Preparing weather guidance"),
    "general-query_weather": ToolActivityLabels("Checking the weather"),
    "general-request_clarification": ToolActivityLabels(
        "Preparing a clarification",
        "Waiting for your answer",
        "Clarification answered",
    ),
    "general-search_web": ToolActivityLabels("Searching the web"),
    "publication-open_pubmed_article": ToolActivityLabels(
        "Opening a PubMed article"
    ),
    "publication-open_study_source": ToolActivityLabels(
        "Opening study evidence"
    ),
    "publication-search_pubmed": ToolActivityLabels("Searching PubMed"),
    "publication-search_study_evidence": ToolActivityLabels(
        "Searching study evidence"
    ),
    "study-design-search": ToolActivityLabels("Searching the study design"),
}

_PREFIXES = {
    "analysis",
    "attachments",
    "custom",
    "dbrag",
    "general",
    "publication",
}


def tool_activity_labels(tool_name: str) -> ToolActivityLabels:
    normalized = str(tool_name or "").strip()
    if normalized in _LABELS:
        labels = _LABELS[normalized]
        return ToolActivityLabels(
            started=labels.started,
            waiting=labels.waiting,
            completed=labels.completed or labels.started,
        )

    words = [word for word in re.split(r"[-_]+", normalized) if word]
    if words and words[0].casefold() in _PREFIXES:
        words = words[1:]
    safe = " ".join(words)[:160].strip() or "Using an agent tool"
    label = safe[0].upper() + safe[1:] if safe else "Using an agent tool"
    return ToolActivityLabels(started=label, completed=label)
