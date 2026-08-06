from __future__ import annotations

import csv
import importlib.util
import io
import math
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke_multi_user_isolation_real.py"
)


def _smoke_module():
    assert SCRIPT_PATH.is_file(), "the real multi-user smoke script is missing"
    spec = importlib.util.spec_from_file_location(
        "smoke_multi_user_isolation_real",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mint_access_token_builds_valid_rs256_cognito_claims() -> None:
    smoke = _smoke_module()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    token = smoke.mint_access_token(
        private_key,
        key_id="smoke-key",
        issuer="http://127.0.0.1:8765",
        app_client_id="smoke-client",
        subject="synthetic-user-a",
        now_epoch=1_800_000_000,
    )

    assert jwt.get_unverified_header(token) == {
        "alg": "RS256",
        "kid": "smoke-key",
        "typ": "JWT",
    }
    claims = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
        issuer="http://127.0.0.1:8765",
        options={"verify_aud": False, "verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "client_id": "smoke-client",
        "exp": 1_800_000_300,
        "iat": 1_800_000_000,
        "iss": "http://127.0.0.1:8765",
        "sub": "synthetic-user-a",
        "token_use": "access",
    }


def test_synthetic_csv_contains_only_declared_fixture_rows() -> None:
    smoke = _smoke_module()

    content = smoke.synthetic_csv_bytes()

    assert list(csv.DictReader(io.StringIO(content.decode("utf-8")))) == [
        {"participant_id": "SYN-001", "exposure": "0", "outcome": "0"},
        {"participant_id": "SYN-002", "exposure": "1", "outcome": "1"},
        {"participant_id": "SYN-003", "exposure": "1", "outcome": "0"},
    ]
    assert b"name" not in content.lower()
    assert b"email" not in content.lower()


def test_assert_secret_absent_scans_files_and_response_bodies(tmp_path: Path) -> None:
    smoke = _smoke_module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "safe.txt").write_text("synthetic fixture", encoding="utf-8")

    smoke.assert_secret_absent(
        "provider-secret",
        paths=[runtime],
        response_bodies=[b'{"configured":true}'],
    )

    (runtime / "checkpoint.db").write_bytes(b"prefix-provider-secret-suffix")
    with pytest.raises(AssertionError, match=r"runtime/checkpoint\.db"):
        smoke.assert_secret_absent(
            "provider-secret",
            paths=[runtime],
            response_bodies=[],
        )

    (runtime / "checkpoint.db").write_bytes(b"safe")
    with pytest.raises(AssertionError, match="response body 0"):
        smoke.assert_secret_absent(
            "provider-secret",
            paths=[runtime],
            response_bodies=[b"provider-secret"],
        )


def test_terminate_all_continues_cleanup_after_one_error() -> None:
    smoke = _smoke_module()
    processes = [object(), object()]
    calls: list[object] = []

    def terminate(process: object) -> str | None:
        calls.append(process)
        if process is processes[1]:
            raise RuntimeError("termination failed")
        return "slow shutdown"

    warnings = smoke.terminate_all(processes, terminator=terminate)

    assert calls == list(reversed(processes))
    assert warnings == ["termination failed", "slow shutdown"]


def test_terminate_all_passes_the_global_deadline() -> None:
    smoke = _smoke_module()
    process = object()
    calls: list[tuple[object, float | None]] = []

    def terminate(current: object, *, deadline: float | None = None) -> None:
        calls.append((current, deadline))

    assert smoke.terminate_all(
        [process],
        terminator=terminate,
        deadline=42.0,
    ) == []
    assert calls == [(process, 42.0)]


def test_child_environment_blocks_dotenv_provider_key_reload() -> None:
    smoke = _smoke_module()

    environment = smoke.child_environment(
        {"OPENAI_API_KEY": "server-owned-key", "PRESERVED": "yes"},
        overrides={"REPORT_AGENT_AUTH_MODE": "cognito"},
    )

    assert environment["OPENAI_API_KEY"] == ""
    assert environment["REPORT_AGENT_AUTH_MODE"] == "cognito"
    assert environment["PRESERVED"] == "yes"


def test_failure_diagnostics_redact_a_detected_secret(tmp_path: Path) -> None:
    smoke = _smoke_module()
    log_path = tmp_path / "api.log"
    log_path.write_text("provider-secret", encoding="utf-8")

    diagnostic = smoke.failure_diagnostic_text(
        RuntimeError("request failed"),
        secret="provider-secret",
        paths=[log_path],
        response_bodies=[],
    )

    assert "Provider key text was found" in diagnostic
    assert "provider-secret" not in diagnostic


def test_smoke_does_not_depend_on_ignored_process_harness() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "e2e_process_harness" not in source


def test_timeout_is_capped_and_expiration_is_enforced() -> None:
    smoke = _smoke_module()

    assert smoke.timeout_seconds("300") == 300
    for invalid in ("301", "nan", "inf", "-inf"):
        with pytest.raises(ValueError, match="no more than 300"):
            smoke.timeout_seconds(invalid)
    assert math.isfinite(smoke.timeout_seconds("1"))
    with pytest.raises(TimeoutError, match="five-minute deadline"):
        smoke.remaining_seconds(10.0, monotonic=lambda: 10.0)
    assert smoke.remaining_seconds(
        20.0,
        cap=3.0,
        monotonic=lambda: 10.0,
    ) == 3.0


def test_five_minute_bound_reserves_time_for_expired_operation_cleanup() -> None:
    smoke = _smoke_module()

    operation_deadline, hard_deadline = smoke.smoke_deadlines(
        started_at=1_000.0,
        timeout=300.0,
    )

    assert operation_deadline == 1_290.0
    assert hard_deadline == 1_300.0
    with pytest.raises(TimeoutError, match="five-minute deadline"):
        smoke.remaining_seconds(operation_deadline, monotonic=lambda: 1_290.0)
    assert smoke.remaining_seconds(
        hard_deadline,
        monotonic=lambda: 1_290.0,
    ) == 10.0
