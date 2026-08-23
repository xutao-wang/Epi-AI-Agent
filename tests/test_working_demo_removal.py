from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_working_demo_has_no_r_runtime_or_review_surface() -> None:
    forbidden_paths = (
        "epi_agent/runtimes/r",
        "epi_agent/tool_packs/analysis/r",
        "epi_agent/tool_packs/epi/r",
        "tools/sandbox/r",
        "frontend/src/CustomRCodeReview.tsx",
        "scripts/e2e_fastapi_typescript_epi_r_real.py",
    )

    assert [
        path
        for path in forbidden_paths
        if (ROOT / path).exists()
    ] == []


def test_active_delivery_sources_have_no_retired_r_tokens() -> None:
    retired_tokens = (
        "custom_r_code_review",
        "analysis-develop_custom_r",
        "REPORT_AGENT_R_SANDBOX_IMAGE",
        "report-agent-r-sandbox",
    )
    candidates = [
        *(ROOT / "api").rglob("*.py"),
        *(ROOT / "db_rag").rglob("*.py"),
        *(ROOT / "epi_agent").rglob("*.py"),
        *(ROOT / "graph").rglob("*.py"),
        *(ROOT / "utils").rglob("*.py"),
        *(ROOT / "frontend" / "src").rglob("*.ts"),
        *(ROOT / "frontend" / "src").rglob("*.tsx"),
        ROOT / "Dockerfile",
        ROOT / "compose.yaml",
        ROOT / "config" / "app.env",
        ROOT / "README.md",
        ROOT / "requirements.txt",
    ]
    offenders = []
    for path in candidates:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in retired_tokens):
            offenders.append(str(path.relative_to(ROOT)))

    assert sorted(set(offenders)) == []


def test_stale_db_rag_error_module_and_streamlit_smoke_are_removed() -> None:
    forbidden_paths = (
        "db_rag/service/errors.py",
        "scripts/e2e_streamlit_db_rag_grouped_review_real.py",
    )

    assert [
        path
        for path in forbidden_paths
        if (ROOT / path).exists()
    ] == []


def test_low_risk_orphan_symbols_are_removed() -> None:
    forbidden_by_path = {
        "db_rag/catalog.py": ("def _bounded_interleave(",),
        "db_rag/config.py": (
            "def shared_env_path_for_project(",
            "def embedding_credentials_ready(",
        ),
        "db_rag/service/models.py": (
            "class DbRagTableHit:",
            "class DbRagColumnHit:",
            "class DbRagContext:",
            "class ColumnSelectionCandidate:",
        ),
        "db_rag/service/schema.py": ("def _lookup_schema_column(",),
        "db_rag/service/sql_service.py": (
            "def _mask_single_quoted_literals_and_comments(",
            "def _contains_sql_identifier(",
            "def _validate_observation_sql(",
        ),
        "epi_agent/registry.py": (
            "ToolContextResolver =",
            "class _RuntimeStructuredTool(",
            "def as_langchain_tools(",
            "def _langchain_function(",
        ),
        "graph/conversation_schema.py": ("ConversationEvent: TypeAlias =",),
        "run_fastapi.py": ("def prepare_provider_credentials(",),
        "tools/execution_policy.py": (
            "class RenderingPolicyError(",
            "def prepare_plotting(",
            "def capture_figure_png(",
        ),
        "tools/mcp_pool.py": ("def query_weather(",),
        "utils/attachment_readers.py": ("def for_conversation(",),
        "utils/dataset_artifacts.py": ("def dataset_artifact_display_label(",),
        "utils/display_history.py": ("def serialize_display_history(",),
        "utils/performance.py": (
            "def append_workflow_timings(",
            "def combined_timing_stages(",
        ),
        "utils/runtime_defaults.py": (
            "AVAILABLE_OPENAI_MODELS =",
            "TEMPERATURE_RANGE =",
            "TEMPERATURE_STEP =",
            "TOP_P_RANGE =",
            "TOP_P_STEP =",
            "MAX_AUTO_STEPS_RANGE =",
            "DEFAULT_EXECUTION_TIMEOUT_SEC =",
            "EXECUTION_TIMEOUT_RANGE =",
            "EXECUTION_TIMEOUT_STEP =",
            "def _availability(",
            "def configured_default_model(",
            "def configured_models(",
            "def configured_openai_models(",
            "def configured_title_model(",
        ),
    }
    offenders: dict[str, list[str]] = {}
    for relative_path, tokens in forbidden_by_path.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        present = [token for token in tokens if token in source]
        if present:
            offenders[relative_path] = present

    assert offenders == {}


def test_db_rag_retrieval_and_reranking_boundary_is_preserved() -> None:
    retrieval = (ROOT / "db_rag" / "retrieval.py").read_text(encoding="utf-8")
    vectorstore = (ROOT / "db_rag" / "vectorstore.py").read_text(encoding="utf-8")

    for token in (
        "class RerankedColumns:",
        "class RetrievedColumns(",
        "def retrieve_single_query(",
        "def retrieve_queries(",
        "def rerank_columns(",
        "def retrieve_context_records_for_probes(",
        "OpenAIReranker",
    ):
        assert token in retrieval
    assert "class OpenAIReranker:" in vectorstore
