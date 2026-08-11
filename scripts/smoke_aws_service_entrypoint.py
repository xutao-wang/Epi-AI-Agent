#!/usr/bin/env python3
"""Import the exact ASGI target configured by the AWS systemd service."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


IMPORT_ASSERTION = """
import importlib
import sys
from fastapi import FastAPI

module_name, separator, attribute_path = sys.argv[1].partition(":")
if not separator or not module_name or not attribute_path:
    raise SystemExit(f"invalid ASGI target: {sys.argv[1]!r}")
application = importlib.import_module(module_name)
for attribute in attribute_path.split("."):
    application = getattr(application, attribute)
if not isinstance(application, FastAPI):
    raise SystemExit(f"{sys.argv[1]} is not a FastAPI application")
"""


def service_asgi_target(service_path: Path) -> str:
    exec_start_lines = [
        line.removeprefix("ExecStart=")
        for line in service_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(exec_start_lines) != 1:
        raise RuntimeError("AWS service must contain exactly one ExecStart directive")
    command = shlex.split(exec_start_lines[0])
    try:
        uvicorn_index = command.index("uvicorn")
        target = command[uvicorn_index + 1]
    except (ValueError, IndexError) as error:
        raise RuntimeError("AWS service ExecStart does not contain a Uvicorn target") from error
    return target


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = service_asgi_target(
        root / "deploy" / "aws" / "systemd" / "epi-agent.service"
    )
    with tempfile.TemporaryDirectory(prefix="epi-agent-asgi-smoke-") as temporary:
        isolated_root = Path(temporary)
        environment = os.environ.copy()
        environment.update(
            {
                "OPENAI_API_KEY": "",
                "REPORT_AGENT_AUTH_MODE": "cognito",
                "REPORT_AGENT_AWS_REGION": "us-east-1",
                "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
                "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "example",
                "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://example.auth.us-east-1.amazoncognito.com/logout",
                "REPORT_AGENT_AUTH_REDIRECT_URI": "https://example.test/auth/callback",
                "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://example.test/",
                "REPORT_AGENT_RUNTIME_ROOT": str(isolated_root / "runtime"),
                "REPORT_AGENT_CHECKPOINT_DB_PATH": str(isolated_root / "runtime" / "agent_memory_fastapi.db"),
                "REPORT_AGENT_STUDY_ROOT": str(isolated_root / "study_data"),
                "REPORT_AGENT_STATIC_DIR": str(root / "frontend" / "dist"),
                "REPORT_AGENT_PYTHON_WORKER_LAUNCHER": "",
                "DB_RAG_EMBEDDING_MODEL": "OpenAI/text-embedding-3-large",
                "DB_RAG_RERANKER_MODEL": "",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", IMPORT_ASSERTION, target],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    if completed.returncode:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode
    print(f"AWS service ASGI entry-point smoke passed: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
