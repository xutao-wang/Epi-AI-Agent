from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from utils.user_storage import UserStorageLayout
from utils.attachment_artifacts import AttachmentError, LocalAttachmentStore
from utils.dataset_artifacts import generated_dataset_artifact_paths


def test_user_storage_hashes_owner_and_keeps_safe_thread_id(tmp_path: Path) -> None:
    scope = UserStorageLayout(tmp_path).thread("external/sub@example", "thread-123")
    owner_hash = hashlib.sha256(b"external/sub@example").hexdigest()

    assert scope.root == (
        tmp_path.resolve() / "users" / owner_hash / "threads" / "thread-123"
    )
    assert "external/sub@example" not in str(scope.root)
    assert scope.datasets == scope.root / "datasets"


def test_user_storage_scopes_same_thread_and_artifact_ids_by_owner(tmp_path: Path) -> None:
    layout = UserStorageLayout(tmp_path)
    alice = layout.thread("alice@example", "thread-1")
    bob = layout.thread("bob@example", "thread-1")

    assert alice.attachments / "attachment-1" != bob.attachments / "attachment-1"
    assert alice.datasets / "dataset-1.parquet" != bob.datasets / "dataset-1.parquet"
    assert alice.root.is_relative_to(tmp_path.resolve() / "users")
    assert bob.root.is_relative_to(tmp_path.resolve() / "users")


@pytest.mark.parametrize("thread_id", ["", " ", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_user_storage_rejects_unsafe_thread_component(tmp_path: Path, thread_id: str) -> None:
    with pytest.raises(ValueError, match="thread_id must be one safe path component"):
        UserStorageLayout(tmp_path).thread("user", thread_id)


def test_attachment_store_cannot_cross_owner_scope(tmp_path: Path) -> None:
    layout = UserStorageLayout(tmp_path)
    alice = layout.thread("alice", "shared-thread")
    bob = layout.thread("bob", "shared-thread")
    store = LocalAttachmentStore(tmp_path)

    staged = store.stage(alice, "cohort.csv", "text/csv", b"id\n1\n")

    assert store._content_path(alice, staged["id"]) != store._content_path(
        bob, staged["id"]
    )
    with pytest.raises(AttachmentError, match="was not found"):
        store.read_bytes(bob, staged["id"])


def test_scoped_staged_attachment_cleanup_uses_owner_and_thread(tmp_path: Path) -> None:
    scope = UserStorageLayout(tmp_path).thread("alice", "thread-1")
    store = LocalAttachmentStore(tmp_path)
    staged = store.stage(scope, "notes.txt", "text/plain", b"notes")
    manifest_path = store._manifest_path(scope, staged["id"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store._atomic_write_json(manifest_path, manifest)

    assert store.cleanup_expired_staged(older_than_seconds=60) == 1
    with pytest.raises(AttachmentError):
        store.require(scope, staged["id"])


def test_scoped_dataset_paths_require_explicit_dataset_root(tmp_path: Path) -> None:
    scope = UserStorageLayout(tmp_path).thread("alice", "thread-1")
    paths = generated_dataset_artifact_paths(
        dataset_root=scope.datasets,
        dataset_id="dataset-1",
    )

    assert paths["path"] == scope.datasets / "dataset-1.parquet"
