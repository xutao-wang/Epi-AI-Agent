from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CORS_ALLOW_ORIGIN_REGEX = r"^http://(127\.0\.0\.1|localhost):\d+$"
_PYTHON_WORKER_PATH = "/usr/local/libexec/epi-agent-python-worker"
_PYTHON_WORKER_LAUNCHER = ("/usr/bin/sudo", "-n", _PYTHON_WORKER_PATH)


def python_worker_launcher(environ: Mapping[str, str]) -> tuple[str, ...] | None:
    configured = str(environ.get("REPORT_AGENT_PYTHON_WORKER_LAUNCHER", ""))
    if not configured.strip():
        return None
    if "\x00" in configured or "\n" in configured or "\r" in configured:
        raise ValueError("REPORT_AGENT_PYTHON_WORKER_LAUNCHER contains unsafe characters")
    try:
        launcher = tuple(shlex.split(configured))
    except ValueError as exc:
        raise ValueError("REPORT_AGENT_PYTHON_WORKER_LAUNCHER is invalid") from exc
    if launcher not in {(_PYTHON_WORKER_PATH,), _PYTHON_WORKER_LAUNCHER}:
        raise ValueError(
            "REPORT_AGENT_PYTHON_WORKER_LAUNCHER must be the fixed worker path"
        )
    return _PYTHON_WORKER_LAUNCHER


@dataclass(frozen=True)
class DeploymentState:
    maintenance_file: Path | None
    release_id: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "DeploymentState":
        configured = str(environ.get("REPORT_AGENT_MAINTENANCE_FILE", "")).strip()
        release_id = str(environ.get("REPORT_AGENT_RELEASE_ID", "development")).strip()
        return cls(
            maintenance_file=Path(configured) if configured else None,
            release_id=release_id or "development",
        )

    def maintenance_enabled(self) -> bool:
        return self.maintenance_file is not None and self.maintenance_file.is_file()


def required_secret_names(auth_mode: str = "local") -> tuple[str, ...]:
    normalized = str(auth_mode or "").strip().lower()
    if normalized == "local":
        return ("OPENAI_API_KEY",)
    if normalized == "cognito":
        return ()
    raise ValueError("auth_mode must be 'local' or 'cognito'")


def native_static_dir(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / "frontend" / "dist"


def native_runtime_root(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / "runtime"


def native_study_root(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / "study_data"


def native_checkpoint_db_path(project_root: str | Path) -> Path:
    return native_runtime_root(project_root) / "agent_memory_fastapi.db"


def runtime_root() -> Path:
    configured = os.getenv("REPORT_AGENT_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "report-agent"


def study_root() -> Path:
    configured = os.getenv("REPORT_AGENT_STUDY_ROOT", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "report-agent-study-data"


def checkpoint_db_path(root: str | Path) -> Path:
    configured = os.getenv("REPORT_AGENT_CHECKPOINT_DB_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(root) / "agent_memory_fastapi.db"


def static_dir() -> Path | None:
    configured = os.getenv("REPORT_AGENT_STATIC_DIR", "").strip()
    if not configured:
        return None
    return Path(configured)


def cors_allow_origin_regex() -> str:
    return (
        os.getenv(
            "REPORT_AGENT_CORS_ALLOW_ORIGIN_REGEX",
            DEFAULT_CORS_ALLOW_ORIGIN_REGEX,
        ).strip()
        or DEFAULT_CORS_ALLOW_ORIGIN_REGEX
    )
