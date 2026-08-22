from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "smoke_hybrid_evidence_retrieval_real.py"


def test_hybrid_retrieval_smoke_uses_real_production_boundaries() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    source = SCRIPT.read_text(encoding="utf-8")
    required = {
        "discover_studies",
        "resolve_embedding_route",
        "bind_session_studies",
        "build_db_rag_tool_registry",
        "build_publication_tool_registry",
        "build_study_design_tool_registry",
        "StateArtifactStore",
        "hybrid_vector_lexical",
        "matched_by",
        "OPENAI_API_KEY",
        "300",
    }
    assert required <= {marker for marker in required if marker in source}
    assert "Fake" not in source
    assert "monkeypatch" not in source
    assert "stub" not in source.casefold()


def test_hybrid_retrieval_smoke_passes_context_by_keyword() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.count("context=context,") == 3


def test_hybrid_retrieval_smoke_reports_a_sanitized_failure_stage() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'stage = "startup"' in source
    assert "failed at {stage}" in source
