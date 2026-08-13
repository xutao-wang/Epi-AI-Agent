from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "smoke_www_apex_redirect_real.py"
SPEC = importlib.util.spec_from_file_location("smoke_www_apex_redirect_real", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_live_smoke_requires_explicit_opt_in(monkeypatch, tmp_path: Path) -> None:
    called = False

    def fake_run_live(artifact_dir: Path) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(smoke, "run_live", fake_run_live)
    assert smoke.main(["--artifact-dir", str(tmp_path)]) == 2
    assert called is False


def test_live_smoke_uses_exact_canonical_redirects() -> None:
    assert smoke.REDIRECT_CASES == (
        (
            "http://www.epiagent.org/?domain_redirect_smoke=www",
            "https://epiagent.org/?domain_redirect_smoke=www",
        ),
        (
            "https://www.epiagent.org/?domain_redirect_smoke=www",
            "https://epiagent.org/?domain_redirect_smoke=www",
        ),
    )


def test_public_config_must_remain_on_the_apex() -> None:
    smoke.assert_public_config(
        {
            "auth_mode": "cognito",
            "cognito": {
                "redirect_uri": "https://epiagent.org/auth/callback",
                "post_logout_redirect_uri": "https://epiagent.org/",
            },
        }
    )
    with pytest.raises(AssertionError):
        smoke.assert_public_config(
            {
                "auth_mode": "cognito",
                "cognito": {
                    "redirect_uri": "https://www.epiagent.org/auth/callback",
                    "post_logout_redirect_uri": "https://epiagent.org/",
                },
            }
        )


def test_failure_diagnostics_are_written_without_secrets(tmp_path: Path) -> None:
    smoke.write_failure(
        tmp_path,
        RuntimeError("public redirect failed"),
        page=None,
        observations={"canonical": "https://epiagent.org/"},
    )
    failure = (tmp_path / "failure.txt").read_text(encoding="utf-8")
    observations = json.loads(
        (tmp_path / "observations.json").read_text(encoding="utf-8")
    )
    assert "public redirect failed" in failure
    assert observations == {"canonical": "https://epiagent.org/"}
