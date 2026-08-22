from __future__ import annotations

import os
from pathlib import Path
import importlib.util

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "smoke_hybrid_evidence_retrieval_real.py"


def _smoke_module():
    spec = importlib.util.spec_from_file_location("hybrid_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert 'stage = "catalog artifact validation"' in source
    assert 'stage = "publication artifact validation"' in source
    assert 'stage = "study-design artifact validation"' in source
    assert "failed at {stage}" in source


def test_hybrid_retrieval_smoke_accepts_publication_string_provenance() -> None:
    content = {
        "retrieval_mode": "hybrid_vector_lexical",
        "embedding": {"available": True},
        "hits": [{"matched_by": "vector,lexical"}],
    }

    assert _smoke_module()._verify_artifact(content, catalog=False) == 1


def test_hybrid_retrieval_smoke_reports_a_safe_nonhybrid_mode() -> None:
    content = {
        "retrieval_mode": "lexical_fallback",
        "embedding": {
            "available": False,
            "reason_code": "EMBEDDING_PROVIDER_UNAVAILABLE",
        },
        "hits": [{"matched_by": "lexical"}],
    }

    with pytest.raises(RuntimeError, match="EMBEDDING_PROVIDER_UNAVAILABLE"):
        _smoke_module()._verify_artifact(content, catalog=False)
