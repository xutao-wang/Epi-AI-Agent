from __future__ import annotations

import os
from pathlib import Path
import tempfile

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def app_env_path_for_project(project_root: str | Path = PROJECT_ROOT) -> Path:
    return Path(project_root) / "config" / "app.env"


def local_env_path_for_project(project_root: str | Path = PROJECT_ROOT) -> Path:
    return Path(project_root) / ".env"


def _load_env_file(path: Path, *, protected_keys: set[str]) -> None:
    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is None or key in protected_keys:
            continue
        os.environ[key] = value


def load_app_environment(project_root: str | Path = PROJECT_ROOT) -> None:
    protected_keys = set(os.environ)
    root = Path(project_root)
    app_path = app_env_path_for_project(root)
    _load_env_file(app_path, protected_keys=protected_keys)
    _load_env_file(local_env_path_for_project(root), protected_keys=protected_keys)


def persist_local_env_values(
    project_root: str | Path,
    values: dict[str, str],
) -> None:
    path = local_env_path_for_project(project_root)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    replacements = {key: str(value) for key, value in values.items()}
    lines: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        key, separator, _value = line.partition("=")
        if separator and key in replacements:
            lines.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines.extend(
        f"{key}={value}"
        for key, value in replacements.items()
        if key not in seen
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write("\n".join(lines).rstrip() + "\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def remove_local_env_values(
    project_root: str | Path,
    keys: set[str],
) -> None:
    """Atomically remove selected assignments from the project-local .env."""
    path = local_env_path_for_project(project_root)
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    lines: list[str] = []
    for line in existing.splitlines():
        key, separator, _value = line.partition("=")
        if separator and key in keys:
            continue
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        content = "\n".join(lines).rstrip()
        temporary.write(f"{content}\n" if content else "")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)
