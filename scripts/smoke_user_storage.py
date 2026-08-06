#!/usr/bin/env python3
"""Exercise owner/thread artifact isolation through production storage APIs."""
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.attachment_artifacts import AttachmentError, LocalAttachmentStore
from utils.user_storage import UserStorageLayout


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        layout = UserStorageLayout(root)
        alice = layout.thread("smoke-alice@example", "thread-1")
        bob = layout.thread("smoke-bob@example", "thread-1")
        store = LocalAttachmentStore(root)
        artifact = store.stage(alice, "cohort.csv", "text/csv", b"id\n1\n")
        assert store.read_bytes(alice, artifact["id"]) == b"id\n1\n"
        try:
            store.read_bytes(bob, artifact["id"])
        except AttachmentError:
            pass
        else:
            raise AssertionError("owner B read owner A attachment")
        assert "smoke-alice@example" not in str(alice.root)
        assert alice.root != bob.root


if __name__ == "__main__":
    main()
