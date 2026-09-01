from pathlib import Path


def test_dataset_schema_metadata_smoke_uses_real_stack() -> None:
    source = Path("scripts/smoke_dataset_schema_metadata_real.py").read_text(
        encoding="utf-8"
    )
    required = (
        "E2EProcessHarness",
        "sync_playwright",
        "/api/threads/",
        "Dataset schema",
        "Raw schema",
        "description",
        "values",
        "300",
    )
    for marker in required:
        assert marker in source

    forbidden = ("mock", "monkeypatch", "route.fulfill", "page.set_content")
    lowered = source.casefold()
    for marker in forbidden:
        assert marker not in lowered

    assert "/api/conversations" in source
    assert "Save Current Thread" not in source
