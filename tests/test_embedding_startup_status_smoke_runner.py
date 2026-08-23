from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.e2e_embedding_startup_status_real import _thread_id_from_attachment_url


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "e2e_embedding_startup_status_real.py"
)


def test_embedding_startup_status_smoke_is_executable_and_real() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    source = SCRIPT.read_text(encoding="utf-8")
    required = {
        "api.app:app",
        "frontend/dist",
        "sync_playwright",
        "OPENAI_API_KEY",
        "embedding_startup_status",
        "hybrid_vector_lexical",
        "lexical_fallback",
        "Embedding startup probe completed",
        "e2e_process_harness",
        "300",
    }
    assert required <= {marker for marker in required if marker in source}
    assert "Fake" not in source
    assert "monkeypatch" not in source
    assert "stub" not in source.casefold()


def test_attachment_response_url_identifies_pending_thread() -> None:
    assert _thread_id_from_attachment_url(
        "http://127.0.0.1:8765/api/threads/thread-123/attachments"
    ) == "thread-123"
    with pytest.raises(ValueError, match="attachment response"):
        _thread_id_from_attachment_url(
            "http://127.0.0.1:8765/api/conversations"
        )
