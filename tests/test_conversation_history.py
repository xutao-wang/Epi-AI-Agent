from pathlib import Path
import sqlite3

import pytest

from api.conversation_history import (
    ConversationHistoryStore,
    OpenAIConversationTitleGenerator,
)


def test_store_lists_created_threads_newest_first(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.6-terra")
    store.create("user-a", "thread-b", model_name="gpt-5.6-luna")

    assert [item.thread_id for item in store.list("user-a")] == ["thread-b", "thread-a"]


def test_store_records_when_a_conversation_is_opened(tmp_path: Path, monkeypatch) -> None:
    timestamps = iter(["2026-07-30T18:00:00+00:00", "2026-07-30T18:24:00+00:00"])
    monkeypatch.setattr(
        ConversationHistoryStore,
        "_now",
        staticmethod(lambda: next(timestamps)),
    )
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.6-terra")

    opened = store.mark_opened("user-a", "thread-a")

    assert opened is not None
    assert opened.last_opened_at == "2026-07-30T18:24:00+00:00"


def test_store_archives_and_restores_a_conversation(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.5")

    archived = store.archive("user-a", "thread-a")

    assert archived is not None
    assert archived.archived_at is not None
    assert store.list("user-a")[0].archived_at is not None

    restored = store.restore("user-a", "thread-a")

    assert restored is not None
    assert restored.archived_at is None


def test_store_deletes_a_conversation_record(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.5")

    assert store.delete("user-a", "thread-a") is True
    assert store.get("user-a", "thread-a") is None
    assert store.delete("user-a", "thread-a") is False


def test_manual_title_cannot_be_overwritten_by_automatic_title(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.6-terra")
    store.rename("user-a", "thread-a", "TB cohort survival analysis")
    store.set_automatic_title("user-a", "thread-a", "Analyze tuberculosis outcomes")

    record = store.get("user-a", "thread-a")
    assert record is not None
    assert record.title == "TB cohort survival analysis"
    assert record.title_source == "manual"


def test_automatic_title_is_normalized_and_bounded(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "memory.db")
    store.create("user-a", "thread-a", model_name="gpt-5.6-terra")
    store.set_automatic_title("user-a", "thread-a", '  "' + "a" * 200 + '."  ')

    record = store.get("user-a", "thread-a")
    assert record is not None
    assert record.title == "a" * 120
    assert record.title_source == "automatic"


def test_conversation_rows_are_owner_scoped(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "history.db")
    store.create("user-a", "shared-thread-id", model_name="gpt-test")
    store.create("user-b", "shared-thread-id", model_name="gpt-test")
    store.rename("user-a", "shared-thread-id", "User A conversation")
    store.set_automatic_title("user-b", "shared-thread-id", "User B conversation")

    user_a = store.get("user-a", "shared-thread-id")
    user_b = store.get("user-b", "shared-thread-id")
    assert user_a is not None
    assert user_b is not None
    assert user_a.title == "User A conversation"
    assert user_b.title == "User B conversation"
    assert [record.thread_id for record in store.list("user-a")] == ["shared-thread-id"]
    assert [record.thread_id for record in store.list("user-b")] == ["shared-thread-id"]

    assert store.rename("user-c", "shared-thread-id", "stolen") is None
    assert store.get("user-a", "shared-thread-id") == user_a
    assert store.mark_opened("user-c", "shared-thread-id") is None
    assert store.get("user-a", "shared-thread-id") == user_a
    assert store.archive("user-c", "shared-thread-id") is None
    assert store.get("user-a", "shared-thread-id") == user_a

    archived_user_a = store.archive("user-a", "shared-thread-id")
    assert archived_user_a is not None
    assert archived_user_a.archived_at is not None
    assert store.restore("user-c", "shared-thread-id") is None
    assert store.get("user-a", "shared-thread-id") == archived_user_a
    assert store.touch("user-c", "shared-thread-id") is None
    assert store.get("user-a", "shared-thread-id") == archived_user_a
    assert store.set_automatic_title("user-c", "shared-thread-id", "stolen") is None
    assert store.get("user-a", "shared-thread-id") == archived_user_a
    assert store.delete("user-c", "shared-thread-id") is False
    assert store.get("user-a", "shared-thread-id") == archived_user_a
    assert store.get("user-b", "shared-thread-id") == user_b


def test_store_migrates_legacy_rows_to_unowned_until_claimed(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-history.db"
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

    store = ConversationHistoryStore(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_history)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(conversation_history)")}
        owner = connection.execute(
            "SELECT owner_user_id FROM conversation_history WHERE thread_id = ?",
            ("legacy-thread",),
        ).fetchone()

    assert "owner_user_id" in columns
    assert "conversation_history_owner_activity_idx" in indexes
    assert owner == (None,)
    assert store.list("local-user") == []
    assert store.claim_unowned("local-user") == 1
    assert [record.thread_id for record in store.list("local-user")] == ["legacy-thread"]
    assert store.claim_unowned("other-user") == 0
    assert store.get("other-user", "legacy-thread") is None


def test_failed_legacy_migration_rolls_back_to_the_original_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-history.db"
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
                "invalid-title-source",
                "gpt-test",
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
                None,
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        ConversationHistoryStore(db_path)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversation_history)")
        }
        row = connection.execute(
            "SELECT thread_id, title_source FROM conversation_history"
        ).fetchone()

    assert tables == {"conversation_history"}
    assert "owner_user_id" not in columns
    assert row == ("legacy-thread", "invalid-title-source")


def test_title_generator_returns_only_normalized_model_title() -> None:
    class _Response:
        content = '  "Analyze TB survival."  '

    class _Model:
        def invoke(self, _messages):
            return _Response()

    generator = OpenAIConversationTitleGenerator(_Model())

    assert generator.generate("Compare TB survival by HIV status") == "Analyze TB survival"


def test_title_generator_extracts_text_from_responses_content_blocks() -> None:
    class _Response:
        content = [
            {"type": "reasoning", "content": []},
            {"type": "text", "text": "RePORT research assistant identity"},
        ]

    class _Model:
        def invoke(self, _messages):
            return _Response()

    generator = OpenAIConversationTitleGenerator(_Model())

    assert generator.generate("Who are you?") == "RePORT research assistant identity"
