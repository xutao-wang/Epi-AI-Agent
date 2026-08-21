from __future__ import annotations

import os
import tempfile
from pathlib import Path


DEFAULT_CORS_ALLOW_ORIGIN_REGEX = r"^http://(127\.0\.0\.1|localhost):\d+$"


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
