#!/usr/bin/env python3
"""Run the bounded real HTTP acceptance smoke for hosted user isolation."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.env_loader import load_app_environment


MAX_TIMEOUT_SECONDS = 300.0
KEY_ID = "multi-user-smoke-key"
APP_CLIENT_ID = "multi-user-smoke-client"
USER_A = "synthetic-smoke-user-a"
USER_B = "synthetic-smoke-user-b"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def timeout_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            "timeout must be greater than zero and no more than 300 seconds"
        )
    return parsed


def remaining_seconds(
    deadline: float,
    *,
    cap: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("The real smoke exceeded its five-minute deadline.")
    return min(cap, remaining)


def smoke_deadlines(*, started_at: float, timeout: float) -> tuple[float, float]:
    hard_deadline = started_at + timeout
    cleanup_budget = min(10.0, max(0.1, timeout / 5.0))
    return hard_deadline - cleanup_budget, hard_deadline


class ManagedProcess:
    def __init__(
        self,
        *,
        name: str,
        process: subprocess.Popen[str],
        log_handle: Any,
        log_path: Path,
    ) -> None:
        self.name = name
        self.process = process
        self.log_handle = log_handle
        self.log_path = log_path


def _find_available_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                candidate.bind((host, port))
            except OSError:
                continue
        return port
    raise RuntimeError(
        f"No available port found from {preferred_port} to {preferred_port + 99}."
    )


def _start_process(
    *,
    name: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return ManagedProcess(
        name=name,
        process=process,
        log_handle=log_handle,
        log_path=log_path,
    )


def _tail_log(path: Path, *, max_chars: int = 4_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError as exc:
        return f"<failed to read {path}: {type(exc).__name__}: {exc}>"


def _assert_processes_alive(processes: Sequence[ManagedProcess]) -> None:
    for managed in processes:
        exit_code = managed.process.poll()
        if exit_code is not None:
            raise AssertionError(
                f"{managed.name} exited with code {exit_code}. "
                f"Log: {managed.log_path}\n{_tail_log(managed.log_path)}"
            )


def _wait_for_http(
    url: str,
    *,
    deadline: float,
    name: str,
    expected_status: int,
    processes: Sequence[ManagedProcess],
) -> None:
    last_error = ""
    while time.monotonic() < deadline:
        _assert_processes_alive(processes)
        try:
            response = requests.get(url, timeout=min(2.0, remaining_seconds(deadline)))
            if response.status_code == expected_status:
                _assert_processes_alive(processes)
                return
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"Timed out waiting for {name} at {url}: {last_error}")


def _terminate_process(
    managed: ManagedProcess | None,
    *,
    deadline: float | None = None,
) -> str | None:
    if managed is None:
        return None
    process = managed.process
    teardown_deadline = deadline if deadline is not None else time.monotonic() + 5.0
    warning: str | None = None

    def send(signum: int) -> None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    try:
        send(signal.SIGTERM)
        graceful_deadline = min(
            teardown_deadline,
            time.monotonic() + max(0.0, teardown_deadline - time.monotonic()) / 2,
        )
        while process.poll() is None and time.monotonic() < graceful_deadline:
            time.sleep(0.02)
        if process.poll() is None:
            send(signal.SIGKILL)
        while process.poll() is None and time.monotonic() < teardown_deadline:
            time.sleep(0.02)
        if process.poll() is None:
            warning = f"{managed.name} did not stop before the global deadline"
    except Exception as exc:
        warning = f"{managed.name} teardown failed: {type(exc).__name__}: {exc}"
        try:
            send(signal.SIGKILL)
        except Exception:
            pass
    finally:
        try:
            managed.log_handle.close()
        except Exception as exc:
            warning = warning or f"{managed.name} log close failed: {exc}"
    return warning


def synthetic_csv_bytes() -> bytes:
    return (
        "participant_id,exposure,outcome\n"
        "SYN-001,0,0\n"
        "SYN-002,1,1\n"
        "SYN-003,1,0\n"
    ).encode("utf-8")


def mint_access_token(
    private_key: rsa.RSAPrivateKey,
    *,
    key_id: str,
    issuer: str,
    app_client_id: str,
    subject: str,
    now_epoch: int | None = None,
) -> str:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    return jwt.encode(
        {
            "iss": issuer,
            "client_id": app_client_id,
            "token_use": "access",
            "iat": now,
            "exp": now + 300,
            "sub": subject,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


def _jwk_document(
    private_key: rsa.RSAPrivateKey,
    *,
    key_id: str,
) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": key_id, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def _iter_files(paths: Iterable[Path]) -> Iterable[tuple[str, Path]]:
    for configured in paths:
        path = Path(configured)
        if path.is_file():
            yield path.name, path
            continue
        if not path.exists():
            continue
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file():
                yield f"{path.name}/{candidate.relative_to(path)}", candidate


def assert_secret_absent(
    secret: str,
    *,
    paths: Iterable[Path],
    response_bodies: Iterable[bytes],
) -> None:
    encoded = str(secret or "").encode("utf-8")
    if not encoded:
        raise ValueError("secret is required for scanning")
    findings: list[str] = []
    for label, path in _iter_files(paths):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise AssertionError(f"Could not scan {label}: {exc}") from exc
        if encoded in content:
            findings.append(label)
    for index, body in enumerate(response_bodies):
        if encoded in body:
            findings.append(f"response body {index}")
    if findings:
        raise AssertionError(
            "Provider key text was found in: " + ", ".join(findings)
        )


def failure_diagnostic_text(
    error: BaseException,
    *,
    secret: str,
    paths: Iterable[Path],
    response_bodies: Iterable[bytes],
) -> str:
    raw = "".join(traceback.format_exception(error))
    try:
        assert_secret_absent(
            secret,
            paths=paths,
            response_bodies=[*response_bodies, raw.encode("utf-8")],
        )
    except AssertionError as leak:
        return f"{leak}\nOriginal failure type: {type(error).__name__}\n"
    return raw


def child_environment(
    base: dict[str, str],
    *,
    overrides: dict[str, str],
) -> dict[str, str]:
    environment = dict(base)
    environment.update(overrides)
    # An explicit empty value prevents utils.env_loader from restoring the
    # local .env key when api.app is imported in the Cognito child process.
    environment["OPENAI_API_KEY"] = ""
    return environment


def terminate_all(
    processes: Sequence[Any],
    *,
    terminator: Callable[..., str | None] = _terminate_process,
    deadline: float | None = None,
) -> list[str]:
    warnings: list[str] = []
    teardown_deadline = (
        min(deadline, time.monotonic() + 5.0)
        if deadline is not None
        else None
    )
    for process in reversed(processes):
        try:
            warning = (
                terminator(process, deadline=teardown_deadline)
                if teardown_deadline is not None
                else terminator(process)
            )
        except Exception as exc:
            warnings.append(str(exc))
            continue
        if warning:
            warnings.append(warning)
    return warnings


def _retain_running(processes: list[ManagedProcess]) -> None:
    processes[:] = [item for item in processes if item.process.poll() is None]


def create_smoke_app():
    """Uvicorn factory: build the production app with the local protocol issuer."""
    if os.environ.get("OPENAI_API_KEY") != "":
        raise RuntimeError("Cognito smoke child must not have a provider key.")
    from api.app import app as application

    if os.environ.get("OPENAI_API_KEY") != "":
        raise RuntimeError("Application startup restored a server provider key.")
    issuer = os.environ["REPORT_AGENT_SMOKE_ISSUER"]
    jwks_url = os.environ["REPORT_AGENT_SMOKE_JWKS_URL"]
    verifier = application.state.token_verifier
    verifier.issuer = issuer.rstrip("/")
    verifier.jwks_client = jwt.PyJWKClient(jwks_url)
    return application


class _JwksHandler(BaseHTTPRequestHandler):
    document: bytes = b"{}"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            content = b'{"status":"ok"}'
            status = 200
        elif self.path in {"/jwks.json", "/.well-known/jwks.json"}:
            content = self.document
            status = 200
        else:
            content = b'{"detail":"Not Found"}'
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, flush=True)


def serve_jwks(*, host: str, port: int, jwks_file: Path) -> int:
    _JwksHandler.document = jwks_file.read_bytes()
    server = ThreadingHTTPServer((host, port), _JwksHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


class _UserHttp:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        session_id: str,
        deadline: float,
        response_bodies: list[bytes],
        response_records: list[dict[str, Any]],
    ) -> None:
        self.base_url = base_url
        self.deadline = deadline
        self.response_bodies = response_bodies
        self.response_records = response_records
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Epi-Session-ID": session_id,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int | Sequence[int] = 200,
        **kwargs: Any,
    ) -> requests.Response:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers={**self.headers, **kwargs.pop("headers", {})},
            timeout=remaining_seconds(self.deadline),
            **kwargs,
        )
        self.response_bodies.append(response.content)
        self.response_records.append(
            {
                "method": method.upper(),
                "path": path,
                "status": response.status_code,
                "body_bytes": len(response.content),
            }
        )
        accepted = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in accepted:
            raise AssertionError(
                f"{method.upper()} {path} returned {response.status_code}; "
                f"expected {sorted(accepted)}: {response.text[:500]}"
            )
        return response


def _start_api(
    *,
    host: str,
    port: int,
    environment: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    return _start_process(
        name="FastAPI",
        args=[
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.smoke_multi_user_isolation_real:create_smoke_app",
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        log_path=log_path,
    )


def _wait_until_attachment_is_bound(
    user: _UserHttp,
    *,
    thread_id: str,
    attachment_id: str,
) -> None:
    last_state: dict[str, Any] = {}
    while True:
        state = user.request("GET", f"/api/threads/{thread_id}/state").json()
        last_state = state
        attachments = [
            attachment
            for message in state.get("conversation", [])
            for attachment in message.get("attachments", [])
        ]
        if any(item.get("id") == attachment_id for item in attachments):
            return
        remaining_seconds(user.deadline, cap=1.0)
        time.sleep(0.2)
        if state.get("run", {}).get("state") == "failed":
            raise AssertionError(f"Provider run failed before binding: {last_state!r}")


def _assert_other_user_denied(
    user_b: _UserHttp,
    *,
    thread_id: str,
    attachment_id: str,
) -> None:
    if user_b.request("GET", "/api/conversations").json() != {"items": []}:
        raise AssertionError("User B could list user A's conversation.")
    checks = [
        ("POST", f"/api/conversations/{thread_id}/open", {}),
        (
            "PATCH",
            f"/api/conversations/{thread_id}",
            {"json": {"title": "Guessed synthetic conversation"}},
        ),
        ("POST", f"/api/conversations/{thread_id}/archive", {}),
        ("POST", f"/api/conversations/{thread_id}/restore", {}),
        ("DELETE", f"/api/conversations/{thread_id}", {}),
        ("GET", f"/api/threads/{thread_id}/state", {}),
        (
            "POST",
            f"/api/threads/{thread_id}/messages",
            {"json": {"text": "Mutate guessed conversation"}},
        ),
        ("POST", f"/api/threads/{thread_id}/reset", {}),
        (
            "POST",
            f"/api/threads/{thread_id}/attachments",
            {"files": {"files": ("guessed.csv", b"id\n9\n", "text/csv")}},
        ),
        ("GET", f"/api/threads/{thread_id}/attachments/{attachment_id}", {}),
        ("DELETE", f"/api/threads/{thread_id}/attachments/{attachment_id}", {}),
        (
            "POST",
            f"/api/threads/{thread_id}/interrupts/guessed-interrupt/resume",
            {"json": {"action": "continue"}},
        ),
        ("GET", f"/api/threads/{thread_id}/export", {}),
        ("GET", f"/api/threads/{thread_id}/export.zip", {}),
    ]
    for method, path, kwargs in checks:
        user_b.request(method, path, expected=404, **kwargs)


def run(args: argparse.Namespace) -> int:
    load_app_environment(REPO_ROOT)
    provider_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not provider_key:
        print(
            "PREREQUISITE FAILED: set OPENAI_API_KEY to run the real "
            "multi-user isolation smoke; no provider is stubbed.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    started_at = time.monotonic()
    deadline, hard_deadline = smoke_deadlines(
        started_at=started_at,
        timeout=args.timeout_seconds,
    )
    artifact_dir = (
        Path(args.artifact_dir).expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="report-multi-user-isolation-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    study_root = artifact_dir / "study-data"
    runtime_root.mkdir(parents=True, exist_ok=True)
    study_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = runtime_root / "agent_memory_fastapi.db"
    response_bodies: list[bytes] = []
    response_records: list[dict[str, Any]] = []
    processes: list[ManagedProcess] = []

    host = "127.0.0.1"
    jwks_port = _find_available_port(host, args.jwks_port)
    api_port = _find_available_port(host, args.api_port)
    if api_port == jwks_port:
        api_port = _find_available_port(host, api_port + 1)
    issuer = f"http://{host}:{jwks_port}"
    jwks_url = f"{issuer}/jwks.json"
    api_url = f"http://{host}:{api_port}"

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks_file = artifact_dir / "jwks.json"
    jwks_file.write_text(
        json.dumps(_jwk_document(private_key, key_id=KEY_ID)),
        encoding="utf-8",
    )
    now_epoch = int(time.time())
    token_a = mint_access_token(
        private_key,
        key_id=KEY_ID,
        issuer=issuer,
        app_client_id=APP_CLIENT_ID,
        subject=USER_A,
        now_epoch=now_epoch,
    )
    token_b = mint_access_token(
        private_key,
        key_id=KEY_ID,
        issuer=issuer,
        app_client_id=APP_CLIENT_ID,
        subject=USER_B,
        now_epoch=now_epoch,
    )

    api_environment = child_environment(
        dict(os.environ),
        overrides={
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_AUTH_MODE": "cognito",
            "REPORT_AGENT_AWS_REGION": "us-east-1",
            "REPORT_AGENT_COGNITO_USER_POOL_ID": "local-smoke-pool",
            "REPORT_AGENT_COGNITO_APP_CLIENT_ID": APP_CLIENT_ID,
            "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": f"{issuer}/logout",
            "REPORT_AGENT_AUTH_REDIRECT_URI": f"{api_url}/auth/callback",
            "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": f"{api_url}/",
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_STUDY_ROOT": str(study_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(checkpoint_path),
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend" / "dist"),
            "REPORT_AGENT_WEB_CONCURRENCY": "1",
            "WEB_CONCURRENCY": "1",
            "REPORT_AGENT_SMOKE_ISSUER": issuer,
            "REPORT_AGENT_SMOKE_JWKS_URL": jwks_url,
        },
    )

    try:
        jwks_process = _start_process(
            name="local JWKS",
            args=[
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve-jwks",
                "--jwks-host",
                host,
                "--jwks-port",
                str(jwks_port),
                "--jwks-file",
                str(jwks_file),
            ],
            cwd=REPO_ROOT,
            env=api_environment,
            log_path=artifact_dir / "jwks.log",
        )
        processes.append(jwks_process)
        _wait_for_http(
            f"{issuer}/health",
            deadline=min(deadline, time.monotonic() + 30),
            name="local JWKS",
            expected_status=200,
            processes=processes,
        )

        api_process = _start_api(
            host=host,
            port=api_port,
            environment=api_environment,
            log_path=artifact_dir / "api-before-restart.log",
        )
        processes.append(api_process)
        _wait_for_http(
            f"{api_url}/api/health",
            deadline=min(deadline, time.monotonic() + 60),
            name="FastAPI before restart",
            expected_status=200,
            processes=processes,
        )

        public_config = requests.get(
            f"{api_url}/api/public-config",
            timeout=remaining_seconds(deadline),
        )
        response_bodies.append(public_config.content)
        response_records.append(
            {
                "method": "GET",
                "path": "/api/public-config",
                "status": public_config.status_code,
                "body_bytes": len(public_config.content),
            }
        )
        public_config.raise_for_status()
        if public_config.json().get("auth_mode") != "cognito":
            raise AssertionError("The real app did not start in Cognito mode.")
        if public_config.json().get("provider_key_required") is not True:
            raise AssertionError("Hosted public config did not require per-user BYOK.")

        def user(token: str, session_id: str) -> _UserHttp:
            return _UserHttp(
                base_url=api_url,
                token=token,
                session_id=session_id,
                deadline=deadline,
                response_bodies=response_bodies,
                response_records=response_records,
            )

        user_a = user(token_a, SESSION_A)
        user_b = user(token_b, SESSION_B)
        for current in (user_a, user_b):
            if current.request("GET", "/api/session/provider-key").json() != {
                "configured": False
            }:
                raise AssertionError("A hosted session started with a provider key.")
            configured = current.request(
                "PUT",
                "/api/session/provider-key",
                json={"api_key": provider_key},
            ).json()
            if configured != {"configured": True}:
                raise AssertionError("The real provider key was not validated.")

        thread_id = str(
            user_a.request("POST", "/api/threads", json={}).json()["thread_id"]
        )
        csv_content = synthetic_csv_bytes()
        uploaded = user_a.request(
            "POST",
            f"/api/threads/{thread_id}/attachments",
            files={"files": ("synthetic-cohort.csv", csv_content, "text/csv")},
        ).json()
        attachment_id = str(uploaded["attachments"][0]["id"])
        user_a.request(
            "POST",
            f"/api/threads/{thread_id}/messages",
            json={
                "text": "Briefly acknowledge this synthetic CSV without analysis.",
                "attachment_ids": [attachment_id],
            },
        )
        _wait_until_attachment_is_bound(
            user_a,
            thread_id=thread_id,
            attachment_id=attachment_id,
        )
        if user_a.request(
            "GET",
            f"/api/threads/{thread_id}/attachments/{attachment_id}",
        ).content != csv_content:
            raise AssertionError("User A could not download the uploaded synthetic CSV.")

        warning = _terminate_process(
            api_process,
            deadline=min(deadline, time.monotonic() + 5.0),
        )
        if warning:
            raise AssertionError(warning)
        processes.remove(api_process)

        api_process = _start_api(
            host=host,
            port=api_port,
            environment=api_environment,
            log_path=artifact_dir / "api-after-restart.log",
        )
        processes.append(api_process)
        _wait_for_http(
            f"{api_url}/api/health",
            deadline=min(deadline, time.monotonic() + 60),
            name="FastAPI after restart",
            expected_status=200,
            processes=processes,
        )

        user_a = user(token_a, SESSION_A)
        user_b = user(token_b, SESSION_B)
        for current in (user_a, user_b):
            status = current.request("GET", "/api/session/provider-key").json()
            if status != {"configured": False}:
                raise AssertionError("Provider key survived the FastAPI restart.")

        conversations = user_a.request("GET", "/api/conversations").json()["items"]
        if thread_id not in {str(item["thread_id"]) for item in conversations}:
            raise AssertionError("User A's conversation did not survive restart.")
        if user_b.request("GET", "/api/conversations").json() != {"items": []}:
            raise AssertionError("User B listed user A's persisted conversation.")

        user_a.request(
            "PUT",
            "/api/session/provider-key",
            json={"api_key": provider_key},
        )
        user_a.request("POST", f"/api/conversations/{thread_id}/open")
        state = user_a.request("GET", f"/api/threads/{thread_id}/state").json()
        persisted_ids = {
            str(attachment["id"])
            for message in state.get("conversation", [])
            for attachment in message.get("attachments", [])
        }
        if attachment_id not in persisted_ids:
            raise AssertionError("The attachment checkpoint did not survive restart.")
        downloaded = user_a.request(
            "GET",
            f"/api/threads/{thread_id}/attachments/{attachment_id}",
        )
        if downloaded.content != csv_content:
            raise AssertionError("The persisted synthetic CSV changed after restart.")

        _assert_other_user_denied(
            user_b,
            thread_id=thread_id,
            attachment_id=attachment_id,
        )
        if user_b.request("GET", "/api/session/provider-key").json() != {
            "configured": False
        }:
            raise AssertionError("User B unexpectedly acquired user A's key status.")

        cleanup_warnings = terminate_all(processes, deadline=hard_deadline)
        _retain_running(processes)
        if cleanup_warnings or processes:
            raise AssertionError("; ".join(cleanup_warnings))

        assert_secret_absent(
            provider_key,
            paths=[
                runtime_root,
                artifact_dir / "api-before-restart.log",
                artifact_dir / "api-after-restart.log",
                artifact_dir / "jwks.log",
            ],
            response_bodies=response_bodies,
        )
        (artifact_dir / "responses.json").write_text(
            json.dumps(response_records, indent=2),
            encoding="utf-8",
        )
        elapsed = time.monotonic() - started_at
        if time.monotonic() > hard_deadline:
            raise TimeoutError("The real smoke exceeded its five-minute deadline.")
        print(
            "PASS real multi-user isolation smoke: restart persistence, key "
            f"reset, owner denial, and secret scan verified in {elapsed:.1f}s; "
            f"artifacts: {artifact_dir}",
            flush=True,
        )
        return 0
    except BaseException as exc:
        cleanup_warnings = terminate_all(processes, deadline=hard_deadline)
        _retain_running(processes)
        if processes:
            cleanup_warnings.extend(
                terminate_all(processes, deadline=hard_deadline)
            )
            _retain_running(processes)
        scan_paths = [
            runtime_root,
            artifact_dir / "api-before-restart.log",
            artifact_dir / "api-after-restart.log",
            artifact_dir / "jwks.log",
        ]
        diagnostic = (
            "Process cleanup did not complete before the hard deadline; "
            "secret scanning was unsafe.\n"
            if processes
            else failure_diagnostic_text(
                exc,
                secret=provider_key,
                paths=scan_paths,
                response_bodies=response_bodies,
            )
        )
        if cleanup_warnings:
            diagnostic += "Cleanup warnings: " + "; ".join(cleanup_warnings) + "\n"
        (artifact_dir / "failure.txt").write_text(
            diagnostic,
            encoding="utf-8",
        )
        (artifact_dir / "responses.json").write_text(
            json.dumps(response_records, indent=2),
            encoding="utf-8",
        )
        print(
            f"FAIL: {type(exc).__name__}; artifacts: {artifact_dir}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        for warning in terminate_all(processes, deadline=hard_deadline):
            print(f"WARN: {warning}", file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded real hosted multi-user isolation smoke."
    )
    parser.add_argument("--api-port", type=int, default=8090)
    parser.add_argument("--jwks-port", type=int, default=8190)
    parser.add_argument("--timeout-seconds", type=timeout_seconds, default=300.0)
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--serve-jwks", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--jwks-host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--jwks-file", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.serve_jwks:
        if not args.jwks_file:
            raise SystemExit("--jwks-file is required with --serve-jwks")
        return serve_jwks(
            host=args.jwks_host,
            port=args.jwks_port,
            jwks_file=Path(args.jwks_file),
        )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
