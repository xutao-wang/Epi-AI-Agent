from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from uuid import uuid4

from api.activity_labels import tool_activity_labels
from api.schemas import ActivityItem, ActivityRun, ActivityRunState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_activity_runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','waiting','completed','cancelled','error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_activity_run_per_thread
ON agent_activity_runs(thread_id)
WHERE state IN ('running','waiting');
CREATE TABLE IF NOT EXISTS agent_activity_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_activity_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('model','tool','review','recovery')),
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','waiting')),
    tool_name TEXT,
    tool_call_id TEXT,
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, sequence),
    UNIQUE(run_id, tool_call_id)
);
"""

_INTERRUPT_LABELS = {
    "dataset_plan_review": (
        "Waiting for dataset plan review",
        "Dataset plan reviewed",
    ),
    "dataset_review": ("Waiting for dataset approval", "Dataset reviewed"),
    "analysis_result_review": (
        "Waiting for analysis review",
        "Analysis reviewed",
    ),
    "agent_clarification": ("Waiting for your answer", "Clarification answered"),
    "model_output_limit": (
        "Waiting for output approval",
        "Output decision received",
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteActivityStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _active_run(
        connection: sqlite3.Connection,
        thread_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT *
            FROM agent_activity_runs
            WHERE thread_id = ? AND state IN ('running', 'waiting')
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()

    @staticmethod
    def _next_sequence(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM agent_activity_items
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return int(row["next_sequence"])

    @staticmethod
    def _touch_run(
        connection: sqlite3.Connection,
        run_id: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "UPDATE agent_activity_runs SET updated_at = ? WHERE id = ?",
            (timestamp, run_id),
        )

    @staticmethod
    def _append_item(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        label: str,
        status: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        timestamp = _now()
        connection.execute(
            """
            INSERT OR IGNORE INTO agent_activity_items (
                id, run_id, sequence, kind, label, status,
                tool_name, tool_call_id, visible, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                uuid4().hex,
                run_id,
                SqliteActivityStore._next_sequence(connection, run_id),
                kind,
                label,
                status,
                tool_name,
                tool_call_id,
                timestamp,
                timestamp,
            ),
        )
        SqliteActivityStore._touch_run(connection, run_id, timestamp)

    def start_run(self, thread_id: str, user_message_id: str) -> str:
        thread_id = str(thread_id or "").strip()
        user_message_id = str(user_message_id or "").strip()
        if not thread_id or not user_message_id:
            raise ValueError("thread_id and user_message_id are required")

        run_id = uuid4().hex
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_activity_runs (
                    id, thread_id, user_message_id, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, thread_id, user_message_id, timestamp, timestamp),
            )
            self._append_item(
                connection,
                run_id=run_id,
                kind="model",
                label="Understanding your request",
                status="running",
            )
        return run_id

    def model_started(self, thread_id: str) -> None:
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            existing = connection.execute(
                """
                SELECT 1
                FROM agent_activity_items
                WHERE run_id = ? AND kind = 'model'
                  AND status = 'running' AND visible = 1
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            if existing is None:
                self._append_item(
                    connection,
                    run_id=run["id"],
                    kind="model",
                    label="Choosing the next step",
                    status="running",
                )

    def model_completed(self, thread_id: str) -> None:
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            item = connection.execute(
                """
                SELECT id
                FROM agent_activity_items
                WHERE run_id = ? AND kind = 'model'
                  AND status = 'running' AND visible = 1
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            if item is None:
                return
            timestamp = _now()
            connection.execute(
                """
                UPDATE agent_activity_items
                SET status = 'completed', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, item["id"]),
            )
            self._touch_run(connection, run["id"], timestamp)

    def tool_started(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            self._append_item(
                connection,
                run_id=run["id"],
                kind="tool",
                label=tool_activity_labels(tool_name).started,
                status="running",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )

    def tool_completed(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        labels = tool_activity_labels(tool_name)
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            timestamp = _now()
            connection.execute(
                """
                UPDATE agent_activity_items
                SET label = ?, status = 'completed', updated_at = ?
                WHERE run_id = ? AND tool_call_id = ? AND visible = 1
                """,
                (labels.completed or labels.started, timestamp, run["id"], tool_call_id),
            )
            self._touch_run(connection, run["id"], timestamp)

    def tool_recoverable_failure(
        self,
        thread_id: str,
        tool_call_id: str,
    ) -> None:
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            timestamp = _now()
            connection.execute(
                """
                UPDATE agent_activity_items
                SET visible = 0, updated_at = ?
                WHERE run_id = ? AND tool_call_id = ?
                """,
                (timestamp, run["id"], tool_call_id),
            )
            running_model = connection.execute(
                """
                SELECT 1
                FROM agent_activity_items
                WHERE run_id = ? AND kind = 'model'
                  AND status = 'running' AND visible = 1
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            if running_model is None:
                self._append_item(
                    connection,
                    run_id=run["id"],
                    kind="model",
                    label="Choosing the next step",
                    status="running",
                )
            else:
                self._touch_run(connection, run["id"], timestamp)

    def mark_waiting(self, thread_id: str, interrupt_type: str) -> None:
        waiting_label, _ = _INTERRUPT_LABELS.get(
            interrupt_type,
            ("Waiting for your input", "Input received"),
        )
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            if run["state"] == "waiting":
                existing_wait = connection.execute(
                    """
                    SELECT 1
                    FROM agent_activity_items
                    WHERE run_id = ? AND status = 'waiting' AND visible = 1
                    LIMIT 1
                    """,
                    (run["id"],),
                ).fetchone()
                if existing_wait is not None:
                    return
            tool_item = connection.execute(
                """
                SELECT id, tool_name
                FROM agent_activity_items
                WHERE run_id = ? AND kind = 'tool'
                  AND status = 'running' AND visible = 1
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            timestamp = _now()
            tool_waiting_label = None
            if tool_item is not None:
                tool_waiting_label = tool_activity_labels(tool_item["tool_name"]).waiting
            if tool_item is not None and tool_waiting_label:
                connection.execute(
                    """
                    UPDATE agent_activity_items
                    SET label = ?, status = 'waiting', updated_at = ?
                    WHERE id = ?
                    """,
                    (tool_waiting_label, timestamp, tool_item["id"]),
                )
            else:
                self._append_item(
                    connection,
                    run_id=run["id"],
                    kind="review",
                    label=waiting_label,
                    status="waiting",
                )
            timestamp = _now()
            connection.execute(
                """
                UPDATE agent_activity_runs
                SET state = 'waiting', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, run["id"]),
            )

    def resume(self, thread_id: str, interrupt_type: str) -> None:
        _, completed_label = _INTERRUPT_LABELS.get(
            interrupt_type,
            ("Waiting for your input", "Input received"),
        )
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None or run["state"] != "waiting":
                return
            item = connection.execute(
                """
                SELECT id
                FROM agent_activity_items
                WHERE run_id = ? AND status = 'waiting' AND visible = 1
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            timestamp = _now()
            if item is not None:
                connection.execute(
                    """
                    UPDATE agent_activity_items
                    SET label = ?, status = 'completed', updated_at = ?
                    WHERE id = ?
                    """,
                    (completed_label, timestamp, item["id"]),
                )
            connection.execute(
                """
                UPDATE agent_activity_runs
                SET state = 'running', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, run["id"]),
            )

    def finish(self, thread_id: str, state: ActivityRunState) -> None:
        if state not in {"completed", "cancelled", "error"}:
            return
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            timestamp = _now()
            if state == "completed":
                item = connection.execute(
                    """
                    SELECT id
                    FROM agent_activity_items
                    WHERE run_id = ? AND kind = 'model'
                      AND status = 'running' AND visible = 1
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (run["id"],),
                ).fetchone()
                if item is not None:
                    connection.execute(
                        """
                        UPDATE agent_activity_items
                        SET status = 'completed', updated_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, item["id"]),
                    )
            else:
                connection.execute(
                    """
                    UPDATE agent_activity_items
                    SET visible = 0, updated_at = ?
                    WHERE run_id = ? AND status = 'running' AND visible = 1
                    """,
                    (timestamp, run["id"]),
                )
            connection.execute(
                """
                UPDATE agent_activity_runs
                SET state = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, timestamp, run["id"]),
            )

    def recover(self, thread_id: str) -> None:
        with self._connect() as connection:
            run = self._active_run(connection, thread_id)
            if run is None:
                return
            timestamp = _now()
            connection.execute(
                """
                UPDATE agent_activity_items
                SET visible = 0, updated_at = ?
                WHERE run_id = ? AND status = 'running' AND visible = 1
                """,
                (timestamp, run["id"]),
            )
            connection.execute(
                """
                UPDATE agent_activity_runs
                SET state = 'running', updated_at = ?
                WHERE id = ?
                """,
                (timestamp, run["id"]),
            )
            self._append_item(
                connection,
                run_id=run["id"],
                kind="recovery",
                label="Resuming your request",
                status="running",
            )

    def list_runs(self, thread_id: str) -> list[ActivityRun]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM agent_activity_runs
                WHERE thread_id = ?
                ORDER BY created_at, rowid
                """,
                (thread_id,),
            ).fetchall()
            runs: list[ActivityRun] = []
            for row in rows:
                item_rows = connection.execute(
                    """
                    SELECT id, sequence, label, status, tool_name,
                           tool_call_id, created_at, updated_at
                    FROM agent_activity_items
                    WHERE run_id = ? AND visible = 1
                    ORDER BY sequence
                    """,
                    (row["id"],),
                ).fetchall()
                runs.append(
                    ActivityRun(
                        id=row["id"],
                        thread_id=row["thread_id"],
                        user_message_id=row["user_message_id"],
                        state=row["state"],
                        activities=[ActivityItem(**dict(item)) for item in item_rows],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return runs

    def delete_thread(self, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_activity_runs WHERE thread_id = ?",
                (thread_id,),
            )
