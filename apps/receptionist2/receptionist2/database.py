from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .session_policy import assess_rollover, extract_topic_terms


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_shared_permissions(self) -> None:
        # Gateway, worker, watchdog, and root helpers share this database
        # through the receptionist2-state group. SQLite creates files 0644
        # minus umask and copies the main file's mode to -wal/-shm, so the
        # main file must be group-writable before SQLite first opens it.
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o660))
        for path in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            try:
                status = path.stat()
                if status.st_uid == os.geteuid() and status.st_mode & 0o060 != 0o060:
                    os.chmod(path, (status.st_mode | 0o660) & 0o7777)
            except OSError:
                continue

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_shared_permissions()
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    provider_session_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('active','archived')),
                    recent_topics_json TEXT NOT NULL DEFAULT '[]',
                    successful_runs INTEGER NOT NULL DEFAULT 0,
                    context_tokens INTEGER,
                    context_window_tokens INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_state (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    active_session_id TEXT REFERENCES sessions(id),
                    verbose INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbound_messages (
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    exact_text TEXT NOT NULL,
                    run_id TEXT,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(telegram_chat_id, telegram_message_id)
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL,
                    cron TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(telegram_user_id, name)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT REFERENCES sessions(id),
                    schedule_id TEXT REFERENCES schedules(id),
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    source TEXT NOT NULL CHECK(source IN (
                        'message','schedule','diagnostic','investigation',
                        'deployment_diagnosis'
                    )),
                    scheduled_for TEXT,
                    idempotency_key TEXT UNIQUE,
                    exact_prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    provider_session_id TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','running','succeeded','failed','interrupted',
                        'cancelled'
                    )),
                    process_id INTEGER,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    final_response TEXT,
                    error TEXT,
                    usage_json TEXT,
                    status_message_id INTEGER,
                    activity TEXT,
                    current_work TEXT,
                    last_event_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    continuation_of TEXT REFERENCES runs(id),
                    deployment_request_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runs_status_created
                    ON runs(status, created_at);

                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    run_id TEXT REFERENCES runs(id),
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    reply_markup_json TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','sending','delivered','failed'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );

                CREATE INDEX IF NOT EXISTS outbox_status_created
                    ON outbox(status, created_at);

                CREATE TABLE IF NOT EXISTS restart_requests (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    timeout_seconds INTEGER,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','running','succeeded','failed'
                    )),
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_alert_state (
                    probe TEXT PRIMARY KEY,
                    condition TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    alerted_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_rollovers (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    exact_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployment_requests (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    repository_path TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    command TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','approved','running','succeeded','failed',
                        'expired'
                    )),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    output TEXT,
                    error TEXT,
                    diagnosis_run_id TEXT REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS deployment_requests_status
                    ON deployment_requests(status, created_at);

                CREATE TABLE IF NOT EXISTS diagnostic_incidents (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    problem TEXT,
                    report TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)")
            }
            if "scheduled_for" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN scheduled_for TEXT"
                )
            connection.commit()

    def ensure_user(
        self,
        user_id: int,
        chat_id: int,
        *,
        default_model: str,
        default_effort: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM user_state WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            if state and state["active_session_id"]:
                connection.execute(
                    """
                    UPDATE user_state
                    SET telegram_chat_id=?, updated_at=?
                    WHERE telegram_user_id=?
                    """,
                    (chat_id, now, user_id),
                )
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?",
                    (state["active_session_id"],),
                ).fetchone()
                connection.commit()
                return dict(session)
            session_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO sessions(
                    id, telegram_user_id, display_name, model, effort,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    "default",
                    default_model,
                    default_effort,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO user_state(
                    telegram_user_id, telegram_chat_id, active_session_id,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_chat_id=excluded.telegram_chat_id,
                    active_session_id=excluded.active_session_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, chat_id, session_id, now),
            )
            connection.commit()
            return dict(
                connection.execute(
                    "SELECT * FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
            )

    def active_session(self, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.* FROM sessions s
                JOIN user_state u ON u.active_session_id=s.id
                WHERE u.telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("No active session")
        return dict(row)

    def create_session(
        self,
        user_id: int,
        chat_id: int,
        *,
        name: str,
        model: str,
        effort: str,
        activate: bool = True,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions(
                    id, telegram_user_id, display_name, model, effort,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (session_id, user_id, name, model, effort, now, now),
            )
            if activate:
                connection.execute(
                    """
                    INSERT INTO user_state(
                        telegram_user_id, telegram_chat_id, active_session_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                        telegram_chat_id=excluded.telegram_chat_id,
                        active_session_id=excluded.active_session_id,
                        updated_at=excluded.updated_at
                    """,
                    (user_id, chat_id, session_id, now),
                )
            connection.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Session not found")
        return dict(row)

    def list_sessions(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE telegram_user_id=? AND status='active'
                    ORDER BY updated_at DESC
                    """,
                    (user_id,),
                )
            ]

    def switch_session(self, user_id: int, prefix: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE telegram_user_id=? AND status='active' AND id LIKE ?
                """,
                (user_id, f"{prefix}%"),
            ).fetchall()
            if len(rows) != 1:
                raise LookupError("Session prefix must identify exactly one session")
            connection.execute(
                """
                UPDATE user_state SET active_session_id=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (rows[0]["id"], utc_now(), user_id),
            )
            connection.commit()
        return dict(rows[0])

    def set_session_runtime(
        self, user_id: int, *, model: str | None = None, effort: str | None = None
    ) -> dict[str, Any]:
        if model is None and effort is None:
            return self.active_session(user_id)
        assignments = []
        values: list[Any] = []
        if model is not None:
            assignments.append("model=?")
            values.append(model)
        if effort is not None:
            assignments.append("effort=?")
            values.append(effort)
        assignments.append("updated_at=?")
        values.append(utc_now())
        with self.connect() as connection:
            session = self.active_session(user_id)
            values.append(session["id"])
            connection.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            connection.commit()
        return self.active_session(user_id)

    def accept_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        default_model: str,
        default_effort: str,
        source: str = "message",
        exact_prompt: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        session = self.ensure_user(
            user_id,
            chat_id,
            default_model=default_model,
            default_effort=default_effort,
        )
        now = utc_now()
        run_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT run_id FROM inbound_messages
                WHERE telegram_chat_id=? AND telegram_message_id=?
                """,
                (chat_id, message_id),
            ).fetchone()
            if existing:
                run = connection.execute(
                    "SELECT * FROM runs WHERE id=?", (existing["run_id"],)
                ).fetchone()
                connection.commit()
                return dict(run), False
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, telegram_user_id, telegram_chat_id,
                    source, exact_prompt, model, effort, provider_session_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    run_id,
                    session["id"],
                    user_id,
                    chat_id,
                    source,
                    exact_prompt if exact_prompt is not None else text,
                    session["model"],
                    session["effort"],
                    session["provider_session_id"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO inbound_messages(
                    telegram_chat_id, telegram_message_id, exact_text, run_id,
                    received_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, message_id, text, run_id, now),
            )
            self._record_topic(connection, session["id"], text)
            connection.commit()
            return self.get_run(run_id), True

    @staticmethod
    def _record_topic(
        connection: sqlite3.Connection, session_id: str, text: str
    ) -> None:
        terms = extract_topic_terms(text)
        if not terms:
            return
        row = connection.execute(
            "SELECT recent_topics_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        try:
            topics = json.loads(row["recent_topics_json"]) if row else []
        except json.JSONDecodeError:
            topics = []
        if not isinstance(topics, list):
            topics = []
        topics = [*topics, terms][-8:]
        connection.execute(
            "UPDATE sessions SET recent_topics_json=? WHERE id=?",
            (json.dumps(topics), session_id),
        )

    def inbound_run(
        self, chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM inbound_messages i
                JOIN runs r ON r.id=i.run_id
                WHERE i.telegram_chat_id=? AND i.telegram_message_id=?
                """,
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def enqueue_scheduled_run(
        self, schedule: dict[str, Any], scheduled_for: str
    ) -> tuple[dict[str, Any], bool]:
        key = f"{schedule['id']}:{scheduled_for}"
        run_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing), False
            state = connection.execute(
                "SELECT telegram_chat_id FROM user_state "
                "WHERE telegram_user_id=?",
                (schedule["telegram_user_id"],),
            ).fetchone()
            if state is None:
                raise RuntimeError("Scheduled task user has no Telegram state")
            session = connection.execute(
                "SELECT provider_session_id FROM sessions WHERE id=?",
                (schedule["session_id"],),
            ).fetchone()
            if session is None:
                raise RuntimeError("Scheduled task session does not exist")
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, schedule_id, telegram_user_id,
                    telegram_chat_id, source, scheduled_for, idempotency_key,
                    exact_prompt, model, effort, provider_session_id, status,
                    created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'schedule', ?, ?, ?, ?, ?, ?, 'queued', ?
                )
                """,
                (
                    run_id,
                    schedule["session_id"],
                    schedule["id"],
                    schedule["telegram_user_id"],
                    state["telegram_chat_id"],
                    scheduled_for,
                    key,
                    schedule["prompt"],
                    schedule["model"],
                    schedule["effort"],
                    session["provider_session_id"],
                    utc_now(),
                ),
            )
            connection.commit()
        return self.get_run(run_id), True

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Run not found")
        return dict(row)

    def claim_next_run(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draining = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM service_state "
                    "WHERE key IN ('draining','deploy_drain')"
                )
            }
            if draining.get("draining") == "1" or draining.get("deploy_drain"):
                connection.commit()
                return None
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status='queued'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE runs
                SET status='running', attempt=attempt+1, started_at=?,
                    finished_at=NULL, error=NULL
                WHERE id=? AND status='queued'
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
            return self.get_run(row["id"]) if updated.rowcount else None

    def set_run_process(self, run_id: str, process_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET process_id=? WHERE id=?",
                (process_id, run_id),
            )
            connection.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        final_response: str = "",
        error: str = "",
        provider_session_id: str | None = None,
        usage: dict[str, Any] | None = None,
        context_tokens: int | None = None,
        context_window_tokens: int | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise LookupError("Run not found")
            connection.execute(
                """
                UPDATE runs
                SET status=?, process_id=NULL, finished_at=?,
                    final_response=?, error=?, usage_json=?,
                    cancel_requested=0
                WHERE id=?
                """,
                (
                    status,
                    now,
                    final_response or None,
                    error or None,
                    json.dumps(usage or {}),
                    run_id,
                ),
            )
            if provider_session_id and run["session_id"]:
                connection.execute(
                    """
                    UPDATE sessions
                    SET provider_session_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (provider_session_id, now, run["session_id"]),
                )
            if run["session_id"] and status == "succeeded":
                connection.execute(
                    """
                    UPDATE sessions
                    SET successful_runs=successful_runs+1,
                        context_tokens=COALESCE(?, context_tokens),
                        context_window_tokens=COALESCE(
                            ?, context_window_tokens
                        )
                    WHERE id=?
                    """,
                    (context_tokens, context_window_tokens, run["session_id"]),
                )
            body = (
                final_response
                if status == "succeeded" and final_response
                else f"❌ Run {run_id[:8]} {status}: {error or 'no response'}"
            )
            connection.execute(
                """
                INSERT INTO outbox(
                    id, telegram_chat_id, run_id, kind, body, status, created_at
                ) VALUES (?, ?, ?, 'run_result', ?, 'pending', ?)
                """,
                (str(uuid.uuid4()), run["telegram_chat_id"], run_id, body, now),
            )
            connection.commit()

    def running_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM runs WHERE status='running'"
                )
            ]

    def queue_summary(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
            )
            result = {str(row["status"]): int(row["count"]) for row in rows}
            pending = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','failed')"
            ).fetchone()[0]
        result["outbox_pending"] = int(pending)
        return result

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, source, model, effort, status, started_at,
                           finished_at, error, created_at
                    FROM runs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]

    def configured_models(self) -> list[str]:
        with self.connect() as connection:
            return [
                str(row["model"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT model FROM sessions WHERE status='active'
                    UNION
                    SELECT DISTINCT model FROM schedules WHERE enabled=1
                    ORDER BY model
                    """
                )
            ]

    def claim_outbox(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM outbox
                WHERE status IN ('pending','failed')
                ORDER BY created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE outbox
                SET status='sending', attempts=attempts+1
                WHERE id=? AND status IN ('pending','failed')
                """,
                (row["id"],),
            )
            connection.commit()
            return dict(row) if updated.rowcount else None

    def finish_outbox(self, outbox_id: str, error: str | None = None) -> None:
        with self.connect() as connection:
            if error:
                connection.execute(
                    "UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                    (error, outbox_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE outbox
                    SET status='delivered', delivered_at=?, last_error=NULL
                    WHERE id=?
                    """,
                    (utc_now(), outbox_id),
                )
            connection.commit()

    def recover_sending_outbox(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status='failed' WHERE status='sending'"
            )
            connection.commit()
            return cursor.rowcount

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO service_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )
            connection.commit()

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM service_state WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def create_restart_request(
        self, user_id: int, chat_id: int, timeout_seconds: int | None
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO restart_requests(
                    id, telegram_user_id, telegram_chat_id, timeout_seconds,
                    status, requested_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (request_id, user_id, chat_id, timeout_seconds, utc_now()),
            )
            connection.commit()
        return self.get_restart_request(request_id)

    def get_restart_request(self, request_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM restart_requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Restart request not found")
        return dict(row)

    def claim_restart_request(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM restart_requests
                WHERE status='pending'
                ORDER BY requested_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE restart_requests
                SET status='running', started_at=?
                WHERE id=? AND status='pending'
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
            return self.get_restart_request(row["id"]) if updated.rowcount else None

    def finish_restart_request(
        self, request_id: str, *, error: str | None = None
    ) -> None:
        with self.connect() as connection:
            request = connection.execute(
                "SELECT * FROM restart_requests WHERE id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise LookupError("Restart request not found")
            status = "failed" if error else "succeeded"
            connection.execute(
                """
                UPDATE restart_requests
                SET status=?, finished_at=?, error=?
                WHERE id=?
                """,
                (status, utc_now(), error, request_id),
            )
            body = (
                f"❌ Receptionist2 restart failed: {error}"
                if error
                else "👍 Receptionist2 restarted and is ready."
            )
            connection.execute(
                """
                INSERT INTO outbox(
                    id, telegram_chat_id, kind, body, status, created_at
                ) VALUES (?, ?, 'restart_result', ?, 'pending', ?)
                """,
                (
                    str(uuid.uuid4()),
                    request["telegram_chat_id"],
                    body,
                    utc_now(),
                ),
            )
            connection.commit()

    def create_schedule(
        self,
        *,
        user_id: int,
        name: str,
        cron: str,
        timezone: str,
        prompt: str,
        model: str,
        effort: str,
        next_run_at: str,
    ) -> dict[str, Any]:
        schedule_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            state = connection.execute(
                """
                SELECT telegram_chat_id FROM user_state
                WHERE telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
            if state is None:
                raise RuntimeError("Scheduled task user has no Telegram state")
            connection.execute(
                """
                INSERT INTO sessions(
                    id, telegram_user_id, display_name, model, effort,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    f"schedule:{name}",
                    model,
                    effort,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO schedules(
                    id, telegram_user_id, session_id, name, cron, timezone,
                    prompt, model, effort, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    user_id,
                    session_id,
                    name,
                    cron,
                    timezone,
                    prompt,
                    model,
                    effort,
                    next_run_at,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Schedule not found")
        return dict(row)

    def list_schedules(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM schedules
                    WHERE telegram_user_id=?
                    ORDER BY name
                    """,
                    (user_id,),
                )
            ]

    def due_schedules(self, now: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM schedules
                    WHERE enabled=1 AND next_run_at<=?
                    ORDER BY next_run_at, id
                    """,
                    (now,),
                )
            ]

    def advance_schedule(
        self, schedule_id: str, *, last_run_at: str, next_run_at: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE schedules
                SET last_run_at=?, next_run_at=?, updated_at=?
                WHERE id=?
                """,
                (last_run_at, next_run_at, utc_now(), schedule_id),
            )
            connection.commit()

    def set_schedule_enabled(
        self, user_id: int, prefix: str, enabled: bool
    ) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE telegram_user_id=? AND id LIKE ?
                """,
                (user_id, f"{prefix}%"),
            ).fetchall()
            if len(rows) != 1:
                raise LookupError("Schedule prefix must identify exactly one task")
            connection.execute(
                "UPDATE schedules SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utc_now(), rows[0]["id"]),
            )
            connection.commit()
        return self.get_schedule(rows[0]["id"])

    def schedule_by_prefix(
        self, user_id: int, prefix: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE telegram_user_id=? AND id LIKE ?
                """,
                (user_id, f"{prefix}%"),
            ).fetchall()
        if len(rows) != 1:
            raise LookupError("Schedule prefix must identify exactly one task")
        return dict(rows[0])

    def create_incident(
        self, chat_id: int, problem: str | None = None
    ) -> dict[str, Any]:
        incident_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnostic_incidents(
                    id, telegram_chat_id, status, problem, created_at
                ) VALUES (?, ?, 'running', ?, ?)
                """,
                (incident_id, chat_id, problem, utc_now()),
            )
            connection.commit()
        return self.get_incident(incident_id)

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM diagnostic_incidents WHERE id=?",
                (incident_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Incident not found")
        return dict(row)

    def finish_incident(
        self, incident_id: str, *, report: str = "", error: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE diagnostic_incidents
                SET status=?, report=?, error=?, finished_at=?
                WHERE id=?
                """,
                (
                    "failed" if error else "succeeded",
                    report or None,
                    error or None,
                    utc_now(),
                    incident_id,
                ),
            )
            connection.commit()

    def provider_alert(self, probe: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_alert_state WHERE probe=?", (probe,)
            ).fetchone()
        return dict(row) if row else None

    def set_provider_alert(
        self,
        probe: str,
        condition: str,
        detail: str,
        *,
        alerted_at: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_alert_state(
                    probe, condition, detail, alerted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(probe) DO UPDATE SET
                    condition=excluded.condition,
                    detail=excluded.detail,
                    alerted_at=excluded.alerted_at,
                    updated_at=excluded.updated_at
                """,
                (probe, condition, detail, alerted_at, utc_now()),
            )
            connection.commit()

    def get_user_state(self, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_state WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("No Telegram state for this user")
        return dict(row)

    def set_verbose(self, user_id: int, enabled: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE user_state SET verbose=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (int(enabled), utc_now(), user_id),
            )
            connection.commit()

    def verbose_for_chat(self, chat_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT verbose FROM user_state WHERE telegram_chat_id=?",
                (chat_id,),
            ).fetchone()
        return bool(row and row["verbose"])

    def get_session_context(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        try:
            raw_topics = json.loads(session["recent_topics_json"] or "[]")
        except json.JSONDecodeError:
            raw_topics = []
        topics = [
            [str(term) for term in topic if isinstance(term, str)]
            for topic in raw_topics
            if isinstance(topic, list)
        ] if isinstance(raw_topics, list) else []
        return {
            "recent_topics": topics,
            "successful_runs": int(session["successful_runs"] or 0),
            "context_tokens": session["context_tokens"],
            "context_window_tokens": session["context_window_tokens"],
        }

    def session_rollover_reason(
        self, session_id: str, prompt: str
    ) -> str | None:
        session = self.get_session(session_id)
        if not session["provider_session_id"]:
            return None
        context = self.get_session_context(session_id)
        return assess_rollover(
            prompt,
            context["recent_topics"],
            context["successful_runs"],
            context["context_tokens"],
            context["context_window_tokens"],
        )

    def create_pending_rollover(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        session_id: str,
        text: str,
        reason: str,
    ) -> dict[str, Any]:
        pending_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM pending_rollovers WHERE created_at<?",
                ((now - timedelta(days=1)).isoformat(),),
            )
            connection.execute(
                """
                INSERT INTO pending_rollovers(
                    id, telegram_user_id, telegram_chat_id,
                    telegram_message_id, session_id, exact_text, reason,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending_id,
                    user_id,
                    chat_id,
                    message_id,
                    session_id,
                    text,
                    reason,
                    now.isoformat(),
                ),
            )
            connection.commit()
        return self.get_pending_rollover(user_id, pending_id)

    def get_pending_rollover(
        self, user_id: int, pending_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pending_rollovers
                WHERE id=? AND telegram_user_id=?
                """,
                (pending_id, user_id),
            ).fetchone()
        if row is None:
            raise LookupError("This session suggestion is no longer available.")
        return dict(row)

    def consume_pending_rollover(
        self, user_id: int, pending_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM pending_rollovers
                WHERE id=? AND telegram_user_id=?
                """,
                (pending_id, user_id),
            ).fetchone()
            if row is None:
                connection.commit()
                raise LookupError(
                    "This session suggestion is no longer available."
                )
            connection.execute(
                "DELETE FROM pending_rollovers WHERE id=?", (pending_id,)
            )
            connection.commit()
        return dict(row)

    def reset_session(self, user_id: int, chat_id: int) -> dict[str, Any]:
        current = self.active_session(user_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET status='archived', updated_at=? "
                "WHERE id=?",
                (utc_now(), current["id"]),
            )
            connection.commit()
        return self.create_session(
            user_id,
            chat_id,
            name="conversation",
            model=current["model"],
            effort=current["effort"],
        )

    def enqueue_run(
        self,
        *,
        session_id: str,
        user_id: int,
        chat_id: int,
        source: str,
        prompt: str,
        continuation_of: str | None = None,
        deployment_request_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                connection.commit()
                raise LookupError("Session not found")
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, telegram_user_id, telegram_chat_id,
                    source, exact_prompt, model, effort, provider_session_id,
                    status, continuation_of, deployment_request_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    user_id,
                    chat_id,
                    source,
                    prompt,
                    session["model"],
                    session["effort"],
                    session["provider_session_id"],
                    continuation_of,
                    deployment_request_id,
                    utc_now(),
                ),
            )
            connection.commit()
        return self.get_run(run_id)

    def request_cancel(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE status='running' "
                "ORDER BY started_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE runs SET cancel_requested=1 WHERE id=?", (row["id"],)
            )
            connection.commit()
        return self.get_run(row["id"])

    def cancel_requested(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def update_run_progress(
        self, run_id: str, *, activity: str, current_work: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runs SET activity=?, current_work=?, last_event_at=?
                WHERE id=?
                """,
                (activity[:200], current_work[:200], utc_now(), run_id),
            )
            connection.commit()

    def set_status_message(self, run_id: str, message_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status_message_id=? WHERE id=?",
                (message_id, run_id),
            )
            connection.commit()

    def latest_unsuccessful_run(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE telegram_user_id=? AND status IN (
                    'succeeded','failed','interrupted','cancelled'
                )
                ORDER BY finished_at DESC, created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if row is None or row["status"] == "succeeded":
            return None
        return dict(row)

    def requeue_run(self, user_id: int, run_id: str) -> dict[str, Any]:
        original = self.get_run(run_id)
        if (
            int(original["telegram_user_id"]) != user_id
            or original["status"] not in {"failed", "interrupted", "cancelled"}
            or not original["session_id"]
        ):
            raise LookupError("That run cannot be retried.")
        return self.enqueue_run(
            session_id=original["session_id"],
            user_id=user_id,
            chat_id=int(original["telegram_chat_id"]),
            source=original["source"],
            prompt=original["exact_prompt"],
            continuation_of=original["continuation_of"],
            deployment_request_id=original["deployment_request_id"],
        )

    def retry_failed_outbox(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status='pending' "
                "WHERE status IN ('failed','sending')"
            )
            connection.commit()
            return cursor.rowcount

    def enqueue_outbox(
        self,
        chat_id: int,
        body: str,
        *,
        kind: str,
        run_id: str | None = None,
        reply_markup: list[list[dict[str, str]]] | None = None,
    ) -> str:
        outbox_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO outbox(
                    id, telegram_chat_id, run_id, kind, body,
                    reply_markup_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    outbox_id,
                    chat_id,
                    run_id,
                    kind,
                    body,
                    json.dumps(reply_markup) if reply_markup else None,
                    utc_now(),
                ),
            )
            connection.commit()
        return outbox_id

    def primary_chat_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT telegram_chat_id FROM user_state "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return int(row["telegram_chat_id"]) if row else None

    def primary_user_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT telegram_user_id FROM user_state "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return int(row["telegram_user_id"]) if row else None

    def import_deployment_request(
        self,
        *,
        request_id: str,
        user_id: int,
        repository_path: str,
        revision: str,
        command: str,
        summary: str,
        created_at: str,
        expires_at: str,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO deployment_requests(
                    id, telegram_user_id, repository_path, revision, command,
                    summary, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    repository_path,
                    revision,
                    command,
                    summary,
                    created_at,
                    expires_at,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def expire_deployment_requests(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='expired', finished_at=?
                WHERE status='pending' AND expires_at<=?
                """,
                (utc_now(), utc_now()),
            )
            connection.commit()
            return cursor.rowcount

    def pending_deployment_requests(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM deployment_requests
                    WHERE status='pending'
                    ORDER BY created_at, id
                    """
                )
            ]

    def transition_deployment_request(
        self, request_id: str, from_status: str, to_status: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE deployment_requests
                SET status=?{", started_at=?" if to_status == "running" else ""}
                WHERE id=? AND status=?
                """,
                (
                    (to_status, utc_now(), request_id, from_status)
                    if to_status == "running"
                    else (to_status, request_id, from_status)
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def finish_deployment_request(
        self,
        request_id: str,
        *,
        status: str,
        exit_code: int | None,
        output: str | None,
        error: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE deployment_requests
                SET status=?, exit_code=?, output=?, error=?, finished_at=?
                WHERE id=?
                """,
                (status, exit_code, output, error, utc_now(), request_id),
            )
            connection.commit()

    def get_deployment_request(self, request_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployment_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Deployment request not found")
        return dict(row)

    def recent_deployment_requests(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM deployment_requests
                    WHERE telegram_user_id=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                )
            ]

    def latest_deployment_request(
        self, user_id: int
    ) -> dict[str, Any] | None:
        rows = self.recent_deployment_requests(user_id, 1)
        return rows[0] if rows else None

    def enqueue_deployment_diagnosis(
        self,
        *,
        request_id: str,
        user_id: int,
        chat_id: int,
        session_id: str,
        prompt: str,
    ) -> tuple[dict[str, Any], bool]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                """
                SELECT * FROM deployment_requests
                WHERE id=? AND telegram_user_id=?
                """,
                (request_id, user_id),
            ).fetchone()
            if request is None or request["status"] != "failed":
                connection.commit()
                raise LookupError("No failed deployment request matches.")
            if request["diagnosis_run_id"]:
                connection.commit()
                return self.get_run(request["diagnosis_run_id"]), False
            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            run_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, telegram_user_id, telegram_chat_id,
                    source, exact_prompt, model, effort, provider_session_id,
                    status, deployment_request_id, created_at
                ) VALUES (
                    ?, ?, ?, ?, 'deployment_diagnosis', ?, ?, ?, ?, 'queued',
                    ?, ?
                )
                """,
                (
                    run_id,
                    session_id,
                    user_id,
                    chat_id,
                    prompt,
                    session["model"],
                    session["effort"],
                    session["provider_session_id"],
                    request_id,
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE deployment_requests SET diagnosis_run_id=? WHERE id=?",
                (run_id, request_id),
            )
            connection.commit()
        return self.get_run(run_id), True

    def prune(self, *, days: int) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.connect() as connection:
            outbox = connection.execute(
                "DELETE FROM outbox WHERE status='delivered' "
                "AND delivered_at<?",
                (cutoff,),
            ).rowcount
            rollovers = connection.execute(
                "DELETE FROM pending_rollovers WHERE created_at<?",
                (cutoff,),
            ).rowcount
            incidents = connection.execute(
                "DELETE FROM diagnostic_incidents WHERE created_at<?",
                (cutoff,),
            ).rowcount
            connection.commit()
        return {
            "outbox": outbox,
            "pending_rollovers": rollovers,
            "diagnostic_incidents": incidents,
        }
