from pathlib import Path

from epi_agent.agent import (
    GENERAL_SYSTEM_PROMPT,
    build_general_system_prompt,
    build_general_epi_agent_registry,
)
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService


class RegistryOnlyPythonRuntime:
    def execute(self, request, datasets):
        raise AssertionError("registry construction must not execute Python")


def attachment_service(tmp_path: Path) -> AttachmentReaderService:
    return AttachmentReaderService(
        LocalAttachmentStore(tmp_path),
        runtime_root=tmp_path,
    )


DBRAG_TOOL_NAMES = {
    "dbrag-open_artifact",
    "dbrag-search_catalog",
    "dbrag-inspect_table",
    "dbrag-find_join_paths",
    "dbrag-profile_relationship",
    "dbrag-save_dataset_plan",
    "dbrag-validate_dataset_plan",
    "dbrag-request_dataset_plan_review",
    "dbrag-validate_and_extract",
    "dbrag-inspect_dataset",
    "dbrag-request_dataset_review",
}
PUBLICATION_TOOL_NAMES = {
    "publication-search_study_evidence",
    "publication-open_study_source",
}
PUBMED_TOOL_NAMES = {
    "publication-search_pubmed",
    "publication-open_pubmed_article",
}

GENERAL_TOOL_NAMES = {
    "general-request_clarification",
    "general-query_weather",
    "general-get_weather_tips",
    "general-search_web",
    "general-calculate",
}
PYTHON_ONLY_RULE = (
    "Python is the only available analysis runtime. Never offer R code or "
    "describe R as an available runtime."
)


def test_general_registry_exposes_db_rag_and_python_without_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    registry = build_general_epi_agent_registry(
        service=attachment_service(tmp_path),
        python_runtime=RegistryOnlyPythonRuntime(),
        runtime_root=tmp_path,
    )
    names = {
        schema["function"]["name"]
        for schema in registry.model_schemas()
    }

    assert DBRAG_TOOL_NAMES <= names
    assert PUBLICATION_TOOL_NAMES <= names
    assert GENERAL_TOOL_NAMES <= names
    assert not (PUBMED_TOOL_NAMES & names)
    assert "analysis-run_custom_python" in names
    assert len(names) == len(registry.tools())


def test_general_prompt_contains_db_review_boundaries() -> None:
    assert "dbrag-request_dataset_plan_review" in GENERAL_SYSTEM_PROMPT
    assert "dbrag-validate_and_extract" in GENERAL_SYSTEM_PROMPT
    assert "dbrag-validate_and_store_sql" not in GENERAL_SYSTEM_PROMPT
    assert "dbrag-execute_approved_sql" not in GENERAL_SYSTEM_PROMPT
    assert "dbrag-request_dataset_review" in GENERAL_SYSTEM_PROMPT
    assert "single reasoning owner" in GENERAL_SYSTEM_PROMPT
    assert "Do not hand results to another agent" in GENERAL_SYSTEM_PROMPT
    assert (
        "general-request_clarification" in GENERAL_SYSTEM_PROMPT
    )
    assert "Use the deterministic quality report to decide whether to revise SQL" in (
        GENERAL_SYSTEM_PROMPT
    )
    assert "call general-request_clarification with fixed options" in (
        GENERAL_SYSTEM_PROMPT
    )


def test_general_prompt_requires_evidence_first_clarification() -> None:
    prompt = " ".join(GENERAL_SYSTEM_PROMPT.split())

    for required in (
        "Before asking any clarification, use applicable registered evidence tools",
        "Ask when human intent or knowledge is genuinely required",
        "Never guess merely to avoid clarification",
        "user input cannot resolve",
        "Do not repeat a clarification",
    ):
        assert required in prompt


def test_general_prompt_declares_python_as_only_analysis_runtime() -> None:
    assert PYTHON_ONLY_RULE in GENERAL_SYSTEM_PROMPT
    assert PYTHON_ONLY_RULE in build_general_system_prompt(
        include_db_rag=False
    )


def test_general_registry_has_no_r_analysis_tools(tmp_path: Path) -> None:
    registry = build_general_epi_agent_registry(
        service=attachment_service(tmp_path),
        python_runtime=RegistryOnlyPythonRuntime(),
        runtime_root=tmp_path,
    )
    names = {
        schema["function"]["name"]
        for schema in registry.model_schemas()
    }

    assert "analysis-run_custom_python" in names
    assert "analysis-develop_custom_r" not in names
    assert "analysis-run_custom_r" not in names


def test_general_registry_and_prompt_omit_db_rag_when_not_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    registry = build_general_epi_agent_registry(
        service=attachment_service(tmp_path),
        python_runtime=RegistryOnlyPythonRuntime(),
        runtime_root=tmp_path,
        include_db_rag=False,
    )
    names = {
        schema["function"]["name"]
        for schema in registry.model_schemas()
    }

    assert not (DBRAG_TOOL_NAMES & names)
    assert PUBLICATION_TOOL_NAMES <= names
    prompt = build_general_system_prompt(include_db_rag=False)
    assert "dbrag-request_dataset_plan_review" not in prompt
    assert "publication-search_study_evidence" in prompt
    assert "publication-search_pubmed" not in prompt
