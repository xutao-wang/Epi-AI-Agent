#!/usr/bin/env python3
"""Exercise the production conversation-history ownership boundary."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.conversation_history import ConversationHistoryStore
from utils.attachment_artifacts import LocalAttachmentStore


def _create_legacy_history(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE conversation_history (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                title_source TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                archived_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_history
            (thread_id, title, title_source, model_name, created_at, updated_at,
             last_opened_at, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-thread",
                "Legacy conversation",
                "automatic",
                "gpt-test",
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
                None,
            ),
        )


def main() -> None:
    with TemporaryDirectory(prefix="conversation-history-smoke-") as temporary_dir:
        db_path = Path(temporary_dir) / "history.db"
        store = ConversationHistoryStore(db_path)
        store.create("smoke-user-a", "shared-thread", model_name="gpt-test")
        store.create("smoke-user-b", "shared-thread", model_name="gpt-test")

        assert store.rename("smoke-user-b", "shared-thread", "User B thread")
        assert store.get("smoke-user-a", "shared-thread").title != "User B thread"
        assert store.archive("smoke-user-a", "shared-thread") is not None
        assert store.get("smoke-user-b", "shared-thread").archived_at is None

        attachments = LocalAttachmentStore(Path(temporary_dir))
        scope_a = attachments.owner_thread_key("smoke-user-a", "shared-thread")
        scope_b = attachments.owner_thread_key("smoke-user-b", "shared-thread")
        uploaded_a = attachments.stage(
            scope_a,
            "a.csv",
            "text/csv",
            b"id\n1\n",
        )
        uploaded_b = attachments.stage(
            scope_b,
            "b.csv",
            "text/csv",
            b"id\n2\n",
        )
        assert attachments.read_bytes(scope_a, uploaded_a["id"]) == b"id\n1\n"
        assert attachments.read_bytes(scope_b, uploaded_b["id"]) == b"id\n2\n"
        attachments.delete_thread(scope_b)
        assert attachments.read_bytes(scope_a, uploaded_a["id"]) == b"id\n1\n"

        legacy_path = Path(temporary_dir) / "legacy.db"
        _create_legacy_history(legacy_path)
        legacy_store = ConversationHistoryStore(legacy_path)
        assert legacy_store.list("local-user") == []
        assert legacy_store.claim_unowned("local-user") == 1
        assert legacy_store.claim_unowned("other-user") == 0
        assert legacy_store.get("other-user", "legacy-thread") is None

    print("Conversation history owner-isolation smoke passed")


if __name__ == "__main__":
    main()
