from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from utils.user_storage import UserStorageLayout
from utils.attachment_artifacts import AttachmentError, LocalAttachmentStore


def test_user_storage_hashes_owner_and_keeps_safe_thread_id(tmp_path: Path) -> None:
    scope = UserStorageLayout(tmp_path).thread("cognito/sub@example", "thread-123")
    owner_hash = hashlib.sha256(b"cognito/sub@example").hexdigest()

    assert scope.root == (
        tmp_path.resolve() / "users" / owner_hash / "threads" / "thread-123"
    )
    assert "cognito/sub@example" not in str(scope.root)
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
