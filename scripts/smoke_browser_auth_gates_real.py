#!/usr/bin/env python3
"""Exercise local and Cognito-like auth flows in the compiled browser UI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e_process_harness import (
    _find_available_port,
    _start_process,
    _terminate_process,
    _wait_for_http,
)
from utils.env_loader import load_app_environment


LOCAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"
TAB_SESSION_STORAGE_KEY = "report-agent.tab-session-id"


def _timeout_seconds(value: str) -> float:
    timeout = float(value)
    if timeout <= 0 or timeout > 300:
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and no more than 300 seconds"
        )
    return timeout


def _remaining_seconds(deadline: float, *, cap: float = 30.0) -> float:
    return max(0.1, min(cap, deadline - time.monotonic()))


def _remaining_ms(deadline: float, *, cap: int = 30_000) -> int:
    return max(1, int(_remaining_seconds(deadline, cap=cap / 1000) * 1000))


def _write_failure_diagnostics(
    *,
    artifact_dir: Path,
    error: BaseException,
    page: Any | None,
    requests_seen: list[dict[str, Any]],
) -> None:
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    (artifact_dir / "failure-requests.json").write_text(
        json.dumps(requests_seen, indent=2),
        encoding="utf-8",
    )
    if page is not None:
        try:
            (artifact_dir / "failure-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass


def _runtime_options() -> dict[str, Any]:
    settings = {
        "model_name": "gpt-5.4",
        "temperature": 0.2,
        "top_p": 1.0,
        "max_steps": 8,
        "timeout_seconds": 120,
        "db_rag_embedding_model": "text-embedding-3-small",
        "db_rag_reranker_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    }
    return {
        "defaults": settings,
        "capabilities": {
            "publication_knowledge": {
                "status": "available",
                "message": "Publication knowledge is available.",
            },
            "db_rag_dataset": {
                "status": "not_configured",
                "message": "DB-RAG is not configured.",
            },
        },
        "models": [
            {
                "id": "gpt-5.4",
                "label": "gpt-5.4 (Standard)",
                "reasoning_tier": "standard",
                "summary": "Smoke-test model.",
                "initial_output_tokens": 8192,
                "automatic_output_token_ceiling": 16384,
                "user_output_token_increment": 8192,
                "absolute_output_token_ceiling": 24576,
                "request_timeout_seconds": 120,
                "workflow_timeout_seconds": 300,
                "automatic_output_cost": "$0.00",
                "incremental_output_cost": "$0.00",
            }
        ],
    }


def _exercise_cognito_flow(
    *,
    artifact_dir: Path,
    browser: Any,
    app_url: str,
    deadline: float,
    requests_seen: list[dict[str, Any]],
) -> None:
    """Drive a real OIDC/PKCE browser flow against an in-process fake issuer."""
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_id = "browser-auth-smoke-key"
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": key_id, "use": "sig", "alg": "RS256"})
    issuer = f"{app_url}/oidc"
    client_id = "browser-auth-smoke-client"
    redirect_uri = f"{app_url}/auth/callback"
    logout_uri = f"{app_url}/"
    fake_provider_key = "smoke-provider-key-not-a-secret"
    flow_events: list[str] = []
    authorization_nonce = ""

    def json_reply(route: Any, body: Any, *, status: int = 200) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body),
        )

    def protected_headers(request: Any) -> None:
        authorization = request.headers.get("authorization", "")
        if authorization != "Bearer smoke-access-token":
            raise AssertionError(
                f"Protected Cognito request lacks bearer token: {request.url}"
            )
        session_id = request.headers.get("x-epi-session-id", "")
        if not session_id or session_id == LOCAL_SESSION_ID:
            raise AssertionError(
                f"Protected Cognito request lacks random tab session: {request.url}"
            )

    def handle_route(route: Any) -> None:
        nonlocal authorization_nonce
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if path == "/api/public-config":
            if "authorization" in request.headers or "x-epi-session-id" in request.headers:
                raise AssertionError("Cognito public config used protected headers.")
            json_reply(
                route,
                {
                    "auth_mode": "cognito",
                    "provider_key_required": True,
                    "cognito": {
                        "authority": issuer,
                        "client_id": client_id,
                        "logout_endpoint": f"{app_url}/logout",
                        "redirect_uri": redirect_uri,
                        "post_logout_redirect_uri": logout_uri,
                    },
                },
            )
            return
        if path == "/oidc/.well-known/openid-configuration":
            json_reply(
                route,
                {
                    "issuer": issuer,
                    "authorization_endpoint": f"{app_url}/authorize",
                    "token_endpoint": f"{app_url}/token",
                    "jwks_uri": f"{app_url}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
            return
        if path == "/jwks":
            json_reply(route, {"keys": [jwk]})
            return
        if path == "/authorize":
            query = parse_qs(parsed.query)
            if query.get("response_type") != ["code"]:
                raise AssertionError(f"Authorization Code flow missing: {query!r}")
            if query.get("code_challenge_method") != ["S256"]:
                raise AssertionError(f"PKCE S256 challenge missing: {query!r}")
            if query.get("scope") != ["openid email"]:
                raise AssertionError(f"Unexpected OIDC scopes: {query!r}")
            authorization_nonce = query.get("nonce", [""])[0]
            callback = query["redirect_uri"][0]
            callback_query = urlencode(
                {"code": "smoke-authorization-code", "state": query["state"][0]}
            )
            flow_events.append("authorize")
            route.fulfill(status=302, headers={"location": f"{callback}?{callback_query}"})
            return
        if path == "/token":
            form = parse_qs(request.post_data or "")
            if form.get("grant_type") != ["authorization_code"]:
                raise AssertionError(f"Authorization code exchange missing: {form!r}")
            if not form.get("code_verifier", [""])[0]:
                raise AssertionError("OIDC callback omitted the PKCE code verifier.")
            now = int(time.time())
            id_token = jwt.encode(
                {
                    "iss": issuer,
                    "aud": client_id,
                    "sub": "browser-smoke-user",
                    "email": "browser-smoke@example.com",
                    "iat": now,
                    "exp": now + 300,
                    "nonce": authorization_nonce,
                },
                private_key,
                algorithm="RS256",
                headers={"kid": key_id},
            )
            flow_events.append("callback-token")
            json_reply(
                route,
                {
                    "access_token": "smoke-access-token",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "scope": "openid email",
                    "id_token": id_token,
                },
            )
            return
        if path == "/api/session/provider-key":
            protected_headers(request)
            flow_events.append(f"provider-key:{request.method}")
            if request.method == "GET":
                json_reply(route, {"configured": False})
                return
            if request.method == "PUT":
                payload = json.loads(request.post_data or "{}")
                if payload != {"api_key": fake_provider_key}:
                    raise AssertionError("Provider key PUT body was malformed.")
                json_reply(route, {"configured": True})
                return
            if request.method == "DELETE":
                route.fulfill(status=204, body="")
                return
        if path == "/api/runtime/options":
            protected_headers(request)
            flow_events.append("runtime-options")
            json_reply(route, _runtime_options())
            return
        if path == "/api/conversations":
            protected_headers(request)
            flow_events.append("conversations")
            json_reply(route, {"items": []})
            return
        if path == "/logout":
            query = parse_qs(parsed.query)
            if query != {"client_id": [client_id], "logout_uri": [logout_uri]}:
                raise AssertionError(f"Cognito logout query is malformed: {query!r}")
            flow_events.append("hosted-logout")
            route.fulfill(status=200, content_type="text/plain", body="signed out")
            return
        route.continue_()

    def record_request(request: Any) -> None:
        if request.url.startswith(app_url):
            requests_seen.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                }
            )

    context.route("**/*", handle_route)
    page.on("request", record_request)
    try:
        page.goto(app_url, wait_until="networkidle", timeout=_remaining_ms(deadline))
        page.get_by_role("button", name="Sign in").click()
        page.get_by_label("OpenAI API key").wait_for(timeout=_remaining_ms(deadline))
        page.get_by_label("OpenAI API key").fill(fake_provider_key)
        page.get_by_role("button", name="Save key").click()
        page.get_by_text("Signed in as browser-smoke@example.com").wait_for(
            timeout=_remaining_ms(deadline)
        )
        if page.get_by_label("OpenAI API key").count():
            raise AssertionError("Provider key remained rendered after validation.")
        storage = page.evaluate(
            "JSON.stringify({session: Object.fromEntries(Object.entries(sessionStorage)), "
            "local: Object.fromEntries(Object.entries(localStorage))})"
        )
        if fake_provider_key in storage:
            raise AssertionError("Provider key was persisted in browser storage.")
        page.get_by_role("button", name="Sign out").click()
        page.wait_for_url(f"{app_url}/logout?**", timeout=_remaining_ms(deadline))

        required = {
            "authorize",
            "callback-token",
            "provider-key:GET",
            "provider-key:PUT",
            "runtime-options",
            "conversations",
            "provider-key:DELETE",
            "hosted-logout",
        }
        missing = required.difference(flow_events)
        if missing:
            raise AssertionError(f"Cognito flow omitted events: {sorted(missing)!r}")
        ordered = [
            "authorize",
            "callback-token",
            "provider-key:PUT",
            "provider-key:DELETE",
            "hosted-logout",
        ]
        positions = [flow_events.index(item) for item in ordered]
        if positions != sorted(positions):
            raise AssertionError(f"Cognito flow occurred out of order: {flow_events!r}")
    except BaseException:
        try:
            (artifact_dir / "failure-cognito-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-cognito-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass
        raise
    finally:
        context.close()


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    deadline = time.monotonic() + args.timeout_seconds
    artifact_dir = (
        Path(args.artifact_dir)
        if args.artifact_dir
        else Path(tempfile.mkdtemp(prefix="report-browser-auth-gates-smoke-"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = artifact_dir / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    load_app_environment(REPO_ROOT)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError(
            "This real local-mode smoke requires an existing OPENAI_API_KEY; "
            "it does not create or persist one."
        )

    subprocess.run(
        ["npm", "--prefix", "frontend", "run", "build"],
        cwd=REPO_ROOT,
        check=True,
        timeout=_remaining_seconds(deadline, cap=60),
    )

    host = "127.0.0.1"
    port = _find_available_port(host, args.api_port)
    app_url = f"http://{host}:{port}"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_AUTH_MODE": "local",
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend" / "dist"),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime_root),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(
                runtime_root / "agent_memory_fastapi.db"
            ),
        }
    )

    process = _start_process(
        name="FastAPI",
        args=[
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=environment,
        log_path=artifact_dir / "api.log",
    )
    page: Any | None = None
    requests_seen: list[dict[str, Any]] = []
    try:
        _wait_for_http(
            f"{app_url}/api/health",
            deadline=min(deadline, time.monotonic() + 60),
            name="FastAPI health",
            expected_status=200,
            processes=[process],
        )

        raw_config = requests.get(
            f"{app_url}/api/public-config",
            timeout=_remaining_seconds(deadline),
        )
        raw_config.raise_for_status()
        if raw_config.json() != {
            "auth_mode": "local",
            "provider_key_required": False,
            "cognito": None,
        }:
            raise AssertionError(f"Unexpected public config: {raw_config.json()!r}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})

                def record_request(request: Any) -> None:
                    if "/api/" in request.url:
                        requests_seen.append(
                            {
                                "url": request.url,
                                "method": request.method,
                                "headers": dict(request.headers),
                            }
                        )

                page.on("request", record_request)
                page.goto(
                    app_url,
                    wait_until="networkidle",
                    timeout=_remaining_ms(deadline),
                )
                page.get_by_label("Model").wait_for(timeout=_remaining_ms(deadline))

                if page.get_by_role("button", name="Sign in").count():
                    raise AssertionError("Local mode rendered the Cognito sign-in gate.")
                if page.get_by_label("OpenAI API key").count():
                    raise AssertionError("Local mode rendered the provider-key gate.")

                page.get_by_role("button", name="Hide sidebar").click()
                page.get_by_role("button", name="Show sidebar").click()
                page.get_by_label("Model").wait_for(timeout=_remaining_ms(deadline))

                public_requests = [
                    item
                    for item in requests_seen
                    if item["url"].endswith("/api/public-config")
                ]
                if len(public_requests) != 1:
                    raise AssertionError(
                        f"Expected one raw public-config request: {requests_seen!r}"
                    )
                public_headers = public_requests[0]["headers"]
                if "authorization" in public_headers or "x-epi-session-id" in public_headers:
                    raise AssertionError(
                        "Public config was fetched with protected-request headers."
                    )

                protected_requests = [
                    item
                    for item in requests_seen
                    if item["url"].endswith("/api/runtime/options")
                    or item["url"].endswith("/api/conversations")
                ]
                if not protected_requests:
                    raise AssertionError("The mounted App made no protected API requests.")
                for item in protected_requests:
                    headers = item["headers"]
                    if headers.get("x-epi-session-id") != LOCAL_SESSION_ID:
                        raise AssertionError(
                            f"Missing fixed local session header: {item!r}"
                        )
                    if "authorization" in headers:
                        raise AssertionError(
                            f"Local request unexpectedly used a bearer token: {item!r}"
                        )

                session_values = page.evaluate(
                    "Object.fromEntries(Object.entries(window.sessionStorage))"
                )
                local_values = page.evaluate(
                    "Object.fromEntries(Object.entries(window.localStorage))"
                )
                if TAB_SESSION_STORAGE_KEY in session_values:
                    raise AssertionError("Local mode persisted a random tab session ID.")
                storage_text = json.dumps(
                    {"session": session_values, "local": local_values}
                ).lower()
                if "api_key" in storage_text or "openai api key" in storage_text:
                    raise AssertionError("Browser storage contains provider-key metadata.")

                raw_runtime = requests.get(
                    f"{app_url}/api/runtime/options",
                    headers={"X-Epi-Session-ID": LOCAL_SESSION_ID},
                    timeout=_remaining_seconds(deadline),
                )
                raw_runtime.raise_for_status()
                if "defaults" not in raw_runtime.json():
                    raise AssertionError("Raw runtime options response is malformed.")

                (artifact_dir / "final-page.txt").write_text(
                    page.locator("body").inner_text(),
                    encoding="utf-8",
                )
                (artifact_dir / "requests.json").write_text(
                    json.dumps(requests_seen, indent=2),
                    encoding="utf-8",
                )
                page.screenshot(
                    path=str(artifact_dir / "final-screenshot.png"),
                    full_page=True,
                )
                _exercise_cognito_flow(
                    artifact_dir=artifact_dir,
                    browser=browser,
                    app_url=app_url,
                    deadline=deadline,
                    requests_seen=requests_seen,
                )
            finally:
                browser.close()

        print(
            "PASS real browser auth-gates smoke; "
            f"artifacts: {artifact_dir}",
            flush=True,
        )
        return 0
    except BaseException as error:
        _write_failure_diagnostics(
            artifact_dir=artifact_dir,
            error=error,
            page=page,
            requests_seen=requests_seen,
        )
        print(
            f"FAIL: {type(error).__name__}: {error}; artifacts: {artifact_dir}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        warning = _terminate_process(process)
        if warning:
            print(f"WARN: {warning}", file=sys.stderr, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real compiled-browser authentication gate smoke."
    )
    parser.add_argument("--api-port", type=int, default=8070)
    parser.add_argument("--timeout-seconds", type=_timeout_seconds, default=180)
    parser.add_argument("--artifact-dir", default="")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
