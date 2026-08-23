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
