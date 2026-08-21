from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from langchain_core.messages import HumanMessage, SystemMessage

from utils.llm_response import coerce_text_content


_UNTITLED = "Untitled conversation"
_ATTACHMENT_ONLY_TITLE = "Attached data analysis"
_MAX_TITLE_LENGTH = 120


@dataclass(frozen=True)
class ConversationSummary:
    thread_id: str
    title: str
    title_source: str
    model_name: str
    created_at: str
    updated_at: str
    last_opened_at: str | None = None
    archived_at: str | None = None
    awaiting_review: bool = False


class ConversationHistoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'conversation_history'"
            ).fetchone()
            if not table_exists:
                self._create_table(connection)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(conversation_history)")
            }
            if "archived_at" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN archived_at TEXT"
                )
            if "last_opened_at" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN last_opened_at TEXT"
                )
                connection.execute(
                    "UPDATE conversation_history SET last_opened_at = updated_at "
                    "WHERE last_opened_at IS NULL"
                )
            if "owner_user_id" not in columns:
                self._migrate_legacy_table(connection)
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(conversation_history)")
                }
            if "lifecycle" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN lifecycle "
                    "TEXT NOT NULL DEFAULT 'ready' "
                    "CHECK(lifecycle IN ('pending', 'ready'))"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS conversation_history_owner_activity_idx "
                "ON conversation_history (owner_user_id, archived_at, updated_at DESC)"
            )

    @staticmethod
    def _create_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE conversation_history (
                owner_user_id TEXT,
                thread_id TEXT NOT NULL,
                title TEXT NOT NULL,
                title_source TEXT NOT NULL CHECK(title_source IN ('automatic', 'manual')),
                model_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT,
                archived_at TEXT,
                lifecycle TEXT NOT NULL DEFAULT 'ready'
                    CHECK(lifecycle IN ('pending', 'ready')),
                PRIMARY KEY (owner_user_id, thread_id)
            )
            """
        )

    @classmethod
    def _migrate_legacy_table(cls, connection: sqlite3.Connection) -> None:
        connection.execute(
            "ALTER TABLE conversation_history RENAME TO conversation_history_legacy"
        )
        cls._create_table(connection)
        connection.execute(
            """
            INSERT INTO conversation_history
            (owner_user_id, thread_id, title, title_source, model_name, created_at,
             updated_at, last_opened_at, archived_at)
            SELECT NULL, thread_id, title, title_source, model_name, created_at,
                   updated_at, last_opened_at, archived_at
            FROM conversation_history_legacy
            """
        )
        connection.execute("DROP TABLE conversation_history_legacy")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _summary(
        row: tuple[str, str, str, str, str, str, str | None, str | None] | None,
    ) -> ConversationSummary | None:
        return ConversationSummary(*row) if row is not None else None

    @staticmethod
    def _title(value: str) -> str:
        normalized = str(value or "").strip().strip("\"'").rstrip(".").strip()
        if not normalized:
            raise ValueError("title is required")
        return normalized[:_MAX_TITLE_LENGTH]

    @staticmethod
    def fallback_title(first_message: str) -> str:
        normalized = " ".join(str(first_message or "").split())
        if not normalized:
            return _ATTACHMENT_ONLY_TITLE
        try:
            return ConversationHistoryStore._title(normalized)
        except ValueError:
            return _ATTACHMENT_ONLY_TITLE

    def create(
        self,
        owner_user_id: str,
        thread_id: str,
        *,
        model_name: str,
    ) -> ConversationSummary:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_history
                (owner_user_id, thread_id, title, title_source, model_name, created_at,
                 updated_at, last_opened_at)
                VALUES (?, ?, ?, 'automatic', ?, ?, ?, ?)
                """,
                (owner_user_id, thread_id, _UNTITLED, model_name, now, now, now),
            )
        record = self.get(owner_user_id, thread_id)
        assert record is not None
        return record

    def create_pending(
        self,
        owner_user_id: str,
        thread_id: str | None = None,
        *,
        model_name: str,
    ) -> tuple[ConversationSummary, bool]:
        if thread_id is None:
            thread_id = owner_user_id
            owner_user_id = "local-user"
        now = self._now()
        with self._connect() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO conversation_history "
                "(owner_user_id, thread_id, title, title_source, model_name, lifecycle, "
                "created_at, updated_at, last_opened_at) "
                "VALUES (?, ?, ?, 'automatic', ?, 'pending', ?, ?, ?)",
                (owner_user_id, thread_id, _UNTITLED, model_name, now, now, now),
            )
        record = self.get(owner_user_id, thread_id)
        assert record is not None
        return record, result.rowcount == 1

    def promote_pending(self, owner_user_id: str, thread_id: str | None = None) -> bool:
        if thread_id is None:
            thread_id = owner_user_id
            owner_user_id = "local-user"
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE conversation_history SET lifecycle = 'ready', updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ? AND lifecycle = 'pending'",
                (self._now(), owner_user_id, thread_id),
            )
        return result.rowcount == 1

    def set_model(
        self,
        owner_user_id: str,
        thread_id: str,
        model_name: str,
    ) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE conversation_history SET model_name = ?, updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (model_name, self._now(), owner_user_id, thread_id),
            )
        return result.rowcount == 1

    def delete_pending(self, owner_user_id: str, thread_id: str | None = None) -> bool:
        if thread_id is None:
            thread_id = owner_user_id
            owner_user_id = "local-user"
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM conversation_history "
                "WHERE owner_user_id = ? AND thread_id = ? AND lifecycle = 'pending'",
                (owner_user_id, thread_id),
            )
        return result.rowcount == 1

    def get(
        self,
        owner_user_id: str,
        thread_id: str | None = None,
    ) -> ConversationSummary | None:
        if thread_id is None:
            thread_id = owner_user_id
            owner_user_id = "local-user"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thread_id, title, title_source, model_name, created_at, updated_at, last_opened_at, archived_at "
                "FROM conversation_history WHERE owner_user_id = ? AND thread_id = ?",
                (owner_user_id, thread_id),
            ).fetchone()
        return self._summary(row)

    def list(self, owner_user_id: str = "local-user") -> list[ConversationSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT thread_id, title, title_source, model_name, created_at, updated_at, last_opened_at, archived_at "
                "FROM conversation_history WHERE owner_user_id = ? AND lifecycle = 'ready' "
                "ORDER BY updated_at DESC, created_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [ConversationSummary(*row) for row in rows]

    def archive(
        self,
        owner_user_id: str,
        thread_id: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET archived_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (self._now(), owner_user_id, thread_id),
            )
        return self.get(owner_user_id, thread_id)

    def restore(
        self,
        owner_user_id: str,
        thread_id: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET archived_at = NULL, updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (self._now(), owner_user_id, thread_id),
            )
        return self.get(owner_user_id, thread_id)

    def delete(self, owner_user_id: str, thread_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM conversation_history "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (owner_user_id, thread_id),
            )
        return result.rowcount == 1

    def touch(self, owner_user_id: str, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (self._now(), owner_user_id, thread_id),
            )

    def mark_opened(
        self,
        owner_user_id: str,
        thread_id: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET last_opened_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (self._now(), owner_user_id, thread_id),
            )
        return self.get(owner_user_id, thread_id)

    def rename(
        self,
        owner_user_id: str,
        thread_id: str,
        title: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, title_source = 'manual', updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ?",
                (self._title(title), self._now(), owner_user_id, thread_id),
            )
        return self.get(owner_user_id, thread_id)

    def set_automatic_title(
        self,
        owner_user_id: str,
        thread_id: str,
        title: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ? "
                "AND title_source = 'automatic'",
                (self._title(title), self._now(), owner_user_id, thread_id),
            )
        return self.get(owner_user_id, thread_id)

    def claim_unowned(self, owner_user_id: str) -> int:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE conversation_history SET owner_user_id = ? "
                "WHERE owner_user_id IS NULL",
                (owner_user_id,),
            )
        return result.rowcount

    def set_initial_automatic_title(
        self,
        owner_user_id: str,
        thread_id: str,
        title: str | None = None,
    ) -> ConversationSummary | None:
        if title is None:
            title = thread_id
            thread_id = owner_user_id
            owner_user_id = "local-user"
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, updated_at = ? "
                "WHERE owner_user_id = ? AND thread_id = ? "
                "AND title = ? AND title_source = 'automatic'",
                (self._title(title), self._now(), owner_user_id, thread_id, _UNTITLED),
            )
        return self.get(owner_user_id, thread_id)


class ConversationTitleGenerator:
    def __init__(self, model: object) -> None:
        self._model = model

    @classmethod
    def from_credentials(
        cls,
        *,
        model_name: str,
        api_key: str,
    ) -> "OpenAIConversationTitleGenerator":
        from llm_vllm import build_openai_llm

        return cls(
            build_openai_llm(
                model_name=model_name,
                api_key=api_key,
            )
        )

    def generate(self, first_message: str) -> str:
        response = self._model.invoke(
            [
                SystemMessage(
                    content=(
                        "Return only a concise 4–8 word title describing the "
                        "user's analysis intent. Do not use quotes, markdown, "
                        "or a trailing period."
                    )
                ),
                HumanMessage(content=first_message),
            ]
        )
        return ConversationHistoryStore._title(coerce_text_content(response.content))


# Deprecated alias kept for older imports.
OpenAIConversationTitleGenerator = ConversationTitleGenerator
