from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def _safe_path_component(value: str, *, label: str) -> str:
    component = str(value or "").strip()
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or "\x00" in component
    ):
        raise ValueError(f"{label} must be one safe path component")
    return component


@dataclass(frozen=True)
class ThreadStorageScope:
    owner_user_id: str
    thread_id: str
    root: Path

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"

    @property
    def datasets(self) -> Path:
        return self.root / "datasets"

    @property
    def figures(self) -> Path:
        return self.root / "figures"

    @property
    def tables(self) -> Path:
        return self.root / "tables"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def execution(self) -> Path:
        return self.root / "execution"


class UserStorageLayout:
    def __init__(self, runtime_root: str | Path):
        self.runtime_root = Path(runtime_root).expanduser().resolve()

    def thread(self, owner_user_id: str, thread_id: str) -> ThreadStorageScope:
        owner = owner_user_id.strip()
        safe_thread = _safe_path_component(thread_id, label="thread_id")
        if not owner:
            raise ValueError("owner_user_id is required")
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        root = self.runtime_root / "users" / owner_hash / "threads" / safe_thread
        return ThreadStorageScope(owner, safe_thread, root)
