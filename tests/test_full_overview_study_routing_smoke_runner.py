from __future__ import annotations

import os
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke_full_overview_study_routing_real.py"
)


def test_routing_smoke_is_executable_and_uses_real_boundaries() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    source = SCRIPT.read_text(encoding="utf-8")
    required = {
        "install_study_archives",
        "discover_studies",
        "create_package_archive",
        "Urban Canopy Luminase Cohort",
        "Agricultural Fermentation Survey",
        "api.app:app",
        "frontend/dist",
        "sync_playwright",
        "OPENAI_API_KEY",
        "general-request_clarification",
        "dbrag-",
        "study-design-search",
        "300",
    }
    assert required <= {marker for marker in required if marker in source}
    assert "Fake" not in source
    assert "monkeypatch" not in source
    assert "stub" not in source.casefold()
    assert "--study-archive" not in source
