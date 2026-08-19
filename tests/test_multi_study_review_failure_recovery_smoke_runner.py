from __future__ import annotations

import os
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke_multi_study_review_failure_recovery_real.py"
)


def test_feature_smoke_is_executable_and_uses_real_production_boundaries() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)

    source = SCRIPT.read_text(encoding="utf-8")
    required_markers = {
        "--report-archive",
        "--nhanes-archive",
        "install_study_archives",
        "api.app:app",
        "frontend/dist",
        "sync_playwright",
        "SqliteSaver",
        "legacy-orphan-call",
        "dataset_plan_review",
        "INTERNAL_TOOL_ERROR",
        "300",
    }

    assert required_markers <= set(
        marker for marker in required_markers if marker in source
    )
    assert "Fake" not in source
    assert "monkeypatch" not in source
