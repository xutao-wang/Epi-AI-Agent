from __future__ import annotations

from pathlib import Path

from api.schemas import RuntimeOptions, RuntimeSettings
from epi_agent.agent import build_general_epi_agent_registry
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService


REPO_ROOT = Path(__file__).resolve().parents[1]


class RegistryOnlyPythonRuntime:
    def execute(self, _request, _datasets):
        raise AssertionError("registry inspection must not execute Python")


def test_retired_semantic_paths_are_absent() -> None:
    retired = [
        REPO_ROOT / "graph" / "routing.py",
        REPO_ROOT / "epi_agent" / "db_rag" / "graph.py",
        REPO_ROOT / "epi_agent" / "db_rag" / "state.py",
        REPO_ROOT / "prompts" / "planner_prompt.py",
        REPO_ROOT / "scripts" / "smoke_report_study_scoping_real.py",
    ]

    assert [str(path.relative_to(REPO_ROOT)) for path in retired if path.exists()] == []
    assert list((REPO_ROOT / "graph" / "nodes").rglob("*.py")) == []


def test_retired_orchestrator_and_fallback_helpers_are_absent() -> None:
    forbidden = (
        "def merge_state_patch(",
        "def sole_study_id(",
        "def _sql_error_code(",
        "def invoke_epi_agent(",
        "def get_artifacts(",
        "agent_status",
    )
    offenders: list[str] = []
    for root_name in ("db_rag", "epi_agent", "graph"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    retired_frontend = (
        REPO_ROOT / "frontend" / "src" / "StructuredOutput.tsx",
        REPO_ROOT / "frontend" / "src" / "StructuredOutput.test.tsx",
    )
    assert offenders == []
    assert [path.name for path in retired_frontend if path.exists()] == []
    styles = (REPO_ROOT / "frontend" / "src" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert ".structured-output-" not in styles


def test_production_has_no_import_or_runtime_reference_to_retired_agents() -> None:
    forbidden = (
        "orchestrator_node",
        "route_by_next_action",
        "build_rag_db_qa_node",
        "build_epi_agent_node",
        "generate_code_node",
        "execute_code_node",
        "epi_agent_handoff",
        "orchestrator-gating-policy.md",
        "graph.nodes",
        "epi_agent.db_rag.graph",
        "epi_agent.db_rag.state",
    )
    offenders: list[str] = []
    for root_name in ("api", "epi_agent", "graph", "prompts"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_production_has_no_stale_report_study_couplings() -> None:
    assert not (
        REPO_ROOT / "db_rag" / "service" / "population_compatibility.py"
    ).exists()

    forbidden = (
        "PAIRED_FORMS",
        "inject_paired_forms",
        "population_compatibility",
        "Off Study Form for Cohort A (Form F99A)",
        "Off Study Form for Cohort B (Form 99B)",
        "Final Outcome Determination Form - Cohort A (Active Pulmonary TB, index case)",
        "Final Outcome Determination Form - Cohort B (Household Contacts)",
    )
    offenders: list[str] = []
    for root_name in ("api", "db_rag", "epi_agent", "graph", "utils"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_production_has_no_retired_class_services_or_legacy_memory() -> None:
    retired_paths = (
        REPO_ROOT / "db_rag" / "service" / "retrieval_service.py",
        REPO_ROOT / "graph" / "memory",
    )
    assert [
        str(path.relative_to(REPO_ROOT))
        for path in retired_paths
        if path.exists()
    ] == []

    forbidden = (
        "DbRagService",
        "DbRagRetrievalMixin",
        "DbRagSqlMixin",
        "ChromaStudyKnowledge",
        "study_knowledge_fingerprint",
        'archive.writestr("memory.json"',
    )
    offenders: list[str] = []
    for root_name in ("api", "db_rag", "epi_agent", "graph", "utils"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_centralized_registry_exposes_only_reviewed_python_execution_tool(
    tmp_path: Path,
) -> None:
    service = AttachmentReaderService(
        LocalAttachmentStore(tmp_path),
        runtime_root=tmp_path,
    )
    registry = build_general_epi_agent_registry(
        service=service,
        python_runtime=RegistryOnlyPythonRuntime(),
        runtime_root=tmp_path,
    )
    names = {
        schema["function"]["name"]
        for schema in registry.model_schemas()
    }

    assert "analysis-run_custom_python" in names
    assert "search_studies" not in names
    assert "generate_code" not in names
    assert "execute_code" not in names


def test_runtime_settings_do_not_expose_retired_code_execution_controls() -> None:
    assert "execution_mode" not in RuntimeSettings.model_fields
    assert "skip_code_execution_review" not in RuntimeSettings.model_fields
    assert "execution_modes" not in RuntimeOptions.model_fields


def test_native_demo_runtime_is_openai_only() -> None:
    assert "provider" not in RuntimeSettings.model_fields
    assert "providers" not in RuntimeOptions.model_fields
    assert "models" in RuntimeOptions.model_fields
