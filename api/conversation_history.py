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


class ConversationHistoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    title_source TEXT NOT NULL CHECK(title_source IN ('automatic', 'manual')),
                    model_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    archived_at TEXT,
                    lifecycle TEXT NOT NULL DEFAULT 'ready'
                        CHECK(lifecycle IN ('pending', 'ready'))
                )
                """
            )
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
            if "lifecycle" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN lifecycle "
                    "TEXT NOT NULL DEFAULT 'ready' "
                    "CHECK(lifecycle IN ('pending', 'ready'))"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

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

    def create(self, thread_id: str, *, model_name: str) -> ConversationSummary:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_history
                (thread_id, title, title_source, model_name, created_at, updated_at, last_opened_at)
                VALUES (?, ?, 'automatic', ?, ?, ?, ?)
                """,
                (thread_id, _UNTITLED, model_name, now, now, now),
            )
        record = self.get(thread_id)
        assert record is not None
        return record

    def create_pending(
        self,
        thread_id: str,
        *,
        model_name: str,
    ) -> tuple[ConversationSummary, bool]:
        now = self._now()
        with self._connect() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO conversation_history "
                "(thread_id, title, title_source, model_name, lifecycle, "
                "created_at, updated_at, last_opened_at) "
                "VALUES (?, ?, 'automatic', ?, 'pending', ?, ?, ?)",
                (thread_id, _UNTITLED, model_name, now, now, now),
            )
        record = self.get(thread_id)
        assert record is not None
        return record, result.rowcount == 1

    def promote_pending(self, thread_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE conversation_history SET lifecycle = 'ready', updated_at = ? "
                "WHERE thread_id = ? AND lifecycle = 'pending'",
                (self._now(), thread_id),
            )
        return result.rowcount == 1

    def delete_pending(self, thread_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM conversation_history "
                "WHERE thread_id = ? AND lifecycle = 'pending'",
                (thread_id,),
            )
        return result.rowcount == 1

    def get(self, thread_id: str) -> ConversationSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thread_id, title, title_source, model_name, created_at, updated_at, last_opened_at, archived_at "
                "FROM conversation_history WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return self._summary(row)

    def list(self) -> list[ConversationSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT thread_id, title, title_source, model_name, created_at, updated_at, last_opened_at, archived_at "
                "FROM conversation_history WHERE lifecycle = 'ready' "
                "ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [ConversationSummary(*row) for row in rows]

    def archive(self, thread_id: str) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET archived_at = ? WHERE thread_id = ?",
                (self._now(), thread_id),
            )
        return self.get(thread_id)

    def restore(self, thread_id: str) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET archived_at = NULL, updated_at = ? "
                "WHERE thread_id = ?",
                (self._now(), thread_id),
            )
        return self.get(thread_id)

    def delete(self, thread_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM conversation_history WHERE thread_id = ?",
                (thread_id,),
            )
        return result.rowcount == 1

    def touch(self, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET updated_at = ? WHERE thread_id = ?",
                (self._now(), thread_id),
            )

    def mark_opened(self, thread_id: str) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET last_opened_at = ? WHERE thread_id = ?",
                (self._now(), thread_id),
            )
        return self.get(thread_id)

    def rename(self, thread_id: str, title: str) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, title_source = 'manual', updated_at = ? "
                "WHERE thread_id = ?",
                (self._title(title), self._now(), thread_id),
            )
        return self.get(thread_id)

    def set_automatic_title(self, thread_id: str, title: str) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, updated_at = ? "
                "WHERE thread_id = ? AND title_source = 'automatic'",
                (self._title(title), self._now(), thread_id),
            )
        return self.get(thread_id)

    def set_initial_automatic_title(
        self,
        thread_id: str,
        title: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_history SET title = ?, updated_at = ? "
                "WHERE thread_id = ? AND title = ? AND title_source = 'automatic'",
                (self._title(title), self._now(), thread_id, _UNTITLED),
            )
        return self.get(thread_id)


class OpenAIConversationTitleGenerator:
    def __init__(self, model: object) -> None:
        self._model = model

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
