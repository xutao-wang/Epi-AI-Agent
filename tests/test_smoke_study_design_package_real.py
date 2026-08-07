from __future__ import annotations

import inspect

import pytest


def test_real_study_design_smoke_requires_archive_and_has_no_stub_flags() -> None:
    from scripts import smoke_study_design_package_real as smoke

    parser = smoke._parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    parsed = parser.parse_args(["--archive", "package.tar.gz"])

    assert parsed.archive.name == "package.tar.gz"
    assert tuple(inspect.signature(smoke.main).parameters) == ("argv",)
    help_text = parser.format_help().casefold()
    assert "--archive" in help_text
    assert "fake" not in help_text
    assert "stub" not in help_text
    assert "bypass" not in help_text
