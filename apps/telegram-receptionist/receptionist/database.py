from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import RepositoryConfig


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def initialize(self, repositories: Iterable[RepositoryConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        configured = tuple(repositories)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS repositories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    absolute_path TEXT NOT NULL UNIQUE,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL REFERENCES repositories(id),
                    provider TEXT NOT NULL,
                    provider_session_id TEXT,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','archived','broken')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    telegram_chat_id INTEGER NOT NULL,
                    status_message_id INTEGER,
                    exact_prompt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','running','succeeded','failed','cancelled','timed_out'
                    )),
                    process_id INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    final_response TEXT,
                    error TEXT,
                    provider_usage_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runs_status_created
                    ON runs(status, created_at);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    provider_event_type TEXT NOT NULL,
                    normalized_event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    telegram_user_id INTEGER PRIMARY KEY,
                    active_repository_id INTEGER REFERENCES repositories(id),
                    active_session_id TEXT REFERENCES sessions(id),
                    verbose INTEGER NOT NULL DEFAULT 0,
                    telegram_chat_id INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployments_seen (
                    deployment_id TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployment_requests (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    repository_path TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    command TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','approved','executing','succeeded','failed',
                        'denied','expired'
                    )),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    notified_at TEXT,
                    approved_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    output TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS wnba_selections (
                    telegram_user_id INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    matchup TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            connection.execute("UPDATE repositories SET enabled=0")
            for repository in configured:
                connection.execute(
                    """
                    INSERT INTO repositories(name, absolute_path, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        absolute_path=excluded.absolute_path,
                        enabled=1
                    """,
                    (repository.name, str(repository.path), utc_now()),
                )
            workspace = connection.execute(
                """
                SELECT id FROM repositories
                WHERE name='workspace' AND enabled=1
                """
            ).fetchone()
            if workspace:
                connection.execute(
                    """
                    UPDATE bot_state
                    SET active_repository_id=?, active_session_id=NULL,
                        updated_at=?
                    WHERE active_repository_id IS NULL OR active_repository_id IN (
                        SELECT id FROM repositories WHERE enabled=0
                    )
                    """,
                    (workspace["id"], utc_now()),
                )
            connection.commit()

    def set_wnba_selection(
        self,
        *,
        user_id: int,
        event_id: str,
        matchup: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> dict[str, Any]:
        timestamp = now or datetime.now(UTC)
        if (
            user_id <= 0
            or not event_id
            or len(event_id) > 128
            or not matchup
            or len(matchup) > 200
            or ttl <= timedelta(0)
            or ttl > timedelta(hours=1)
        ):
            raise ValueError("invalid WNBA selection")
        record = {
            "telegram_user_id": user_id,
            "event_id": event_id,
            "matchup": matchup,
            "created_at": timestamp.isoformat(),
            "expires_at": (timestamp + ttl).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO wnba_selections(
                    telegram_user_id, event_id, matchup, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    matchup=excluded.matchup,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                tuple(record.values()),
            )
            connection.commit()
        return record

    def get_wnba_selection(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        timestamp = now or datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM wnba_selections
                WHERE telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            expires_at = datetime.fromisoformat(record["expires_at"])
            if expires_at <= timestamp:
                connection.execute(
                    """
                    DELETE FROM wnba_selections
                    WHERE telegram_user_id=?
                    """,
                    (user_id,),
                )
                connection.commit()
                return None
            return record

    def cancel_wnba_selection(self, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM wnba_selections
                WHERE telegram_user_id=?
                """,
                (user_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

    def recover_interrupted_runs(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status='failed', finished_at=?, process_id=NULL,
                    error='Receptionist restarted while this run was active.'
                WHERE status='running'
                """,
                (utc_now(),),
            )
            connection.commit()
            return cursor.rowcount

    def recover_interrupted_deployments(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='failed', finished_at=?,
                    error='Receptionist restarted during deployment execution.'
                WHERE status IN ('approved','executing')
                """,
                (utc_now(),),
            )
            connection.commit()
            return cursor.rowcount

    def prune_events(self, days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM events WHERE created_at < ?", (cutoff,)
            )
            connection.commit()
            return cursor.rowcount

    def ensure_user_state(self, user_id: int, chat_id: int) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            repository = connection.execute(
                "SELECT id FROM repositories WHERE enabled=1 ORDER BY id LIMIT 1"
            ).fetchone()
            if repository is None:
                raise RuntimeError("no enabled repositories")
            connection.execute(
                """
                INSERT INTO bot_state(
                    telegram_user_id, active_repository_id, verbose,
                    telegram_chat_id, updated_at
                ) VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_chat_id=excluded.telegram_chat_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, repository["id"], chat_id, now),
            )
            connection.commit()
        return self.get_user_state(user_id)

    def get_user_state(self, user_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT bs.*, r.name AS repository_name,
                       r.absolute_path AS repository_path,
                       s.display_name AS session_name,
                       s.provider AS session_provider,
                       s.provider_session_id
                FROM bot_state bs
                LEFT JOIN repositories r ON r.id=bs.active_repository_id
                LEFT JOIN sessions s ON s.id=bs.active_session_id
                WHERE bs.telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("user state is not initialized")
            return dict(row)

    def create_session(
        self, user_id: int, display_name: str | None = None, provider: str = "claude"
    ) -> dict[str, Any]:
        state = self.get_user_state(user_id)
        repository_id = state["active_repository_id"]
        session_id = str(uuid.uuid4())
        now = utc_now()
        name = display_name or f"{state['repository_name']} {now[5:16]}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    id, telegram_user_id, repository_id, provider, display_name,
                    status, created_at, updated_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    repository_id,
                    provider,
                    name[:100],
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE bot_state SET active_session_id=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (session_id, now, user_id),
            )
            connection.commit()
        return self.get_session(session_id)

    def get_or_create_active_session(self, user_id: int) -> dict[str, Any]:
        state = self.get_user_state(user_id)
        if state["active_session_id"]:
            return self.get_session(state["active_session_id"])
        return self.create_session(user_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, r.name AS repository_name,
                       r.absolute_path AS repository_path
                FROM sessions s JOIN repositories r ON r.id=s.repository_id
                WHERE s.id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise LookupError("session not found")
            return dict(row)

    def list_sessions(self, user_id: int, repository_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE telegram_user_id=? AND repository_id=?
                    ORDER BY last_used_at DESC LIMIT 20
                    """,
                    (user_id, repository_id),
                )
            ]

    def switch_session(self, user_id: int, prefix: str) -> dict[str, Any]:
        state = self.get_user_state(user_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE telegram_user_id=? AND repository_id=? AND id LIKE ?
                """,
                (user_id, state["active_repository_id"], f"{prefix}%"),
            ).fetchall()
            if len(rows) != 1:
                raise LookupError("session prefix must match exactly one session")
            session = rows[0]
            connection.execute(
                """
                UPDATE bot_state SET active_session_id=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (session["id"], utc_now(), user_id),
            )
            connection.commit()
            return dict(session)

    def reset_session(self, user_id: int) -> dict[str, Any]:
        state = self.get_user_state(user_id)
        with self._connect() as connection:
            if state["active_session_id"]:
                connection.execute(
                    """
                    UPDATE sessions SET status='archived', updated_at=?
                    WHERE id=?
                    """,
                    (utc_now(), state["active_session_id"]),
                )
                connection.commit()
        return self.create_session(user_id)

    def set_verbose(self, user_id: int, verbose: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE bot_state SET verbose=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (int(verbose), utc_now(), user_id),
            )
            connection.commit()

    def enqueue_run(
        self, session_id: str, chat_id: int, exact_prompt: str
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, telegram_chat_id, exact_prompt, status, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (run_id, session_id, chat_id, exact_prompt, utc_now()),
            )
            connection.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT runs.*, sessions.telegram_user_id, sessions.provider,
                       sessions.provider_session_id, sessions.repository_id,
                       repositories.name AS repository_name,
                       repositories.absolute_path AS repository_path
                FROM runs
                JOIN sessions ON sessions.id=runs.session_id
                JOIN repositories ON repositories.id=sessions.repository_id
                WHERE runs.id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise LookupError("run not found")
            return dict(row)

    def queued_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status='queued'"
                ).fetchone()[0]
            )

    def active_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs WHERE status='running'
                ORDER BY started_at LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def next_queued_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM runs WHERE status='queued'
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            return self.get_run(row["id"]) if row else None

    def start_run(self, run_id: str, process_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs SET status='running', process_id=?, started_at=?
                WHERE id=? AND status='queued'
                """,
                (process_id, utc_now(), run_id),
            )
            connection.commit()

    def set_status_message(self, run_id: str, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status_message_id=? WHERE id=?",
                (message_id, run_id),
            )
            connection.commit()

    def add_event(
        self,
        run_id: str,
        sequence: int,
        provider_type: str,
        normalized_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(
                    run_id, sequence, provider_event_type,
                    normalized_event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    provider_type,
                    normalized_type,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )
            connection.commit()

    def update_provider_session(self, session_id: str, provider_session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions SET provider_session_id=?, updated_at=?,
                    last_used_at=? WHERE id=?
                """,
                (provider_session_id, utc_now(), utc_now(), session_id),
            )
            connection.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        exit_code: int | None,
        final_response: str | None,
        error: str | None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT session_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            connection.execute(
                """
                UPDATE runs SET status=?, process_id=NULL, finished_at=?,
                    exit_code=?, final_response=?, error=?,
                    provider_usage_json=?
                WHERE id=?
                """,
                (
                    status,
                    utc_now(),
                    exit_code,
                    final_response,
                    error,
                    json.dumps(usage) if usage else None,
                    run_id,
                ),
            )
            if run:
                connection.execute(
                    """
                    UPDATE sessions SET updated_at=?, last_used_at=?
                    WHERE id=?
                    """,
                    (utc_now(), utc_now(), run["session_id"]),
                )
            connection.commit()

    def last_run_for_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT runs.* FROM runs
                JOIN sessions ON sessions.id=runs.session_id
                WHERE sessions.telegram_user_id=?
                ORDER BY runs.created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def deployment_is_seen(self, deployment_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM deployments_seen WHERE deployment_id=?",
                (deployment_id,),
            ).fetchone()
            return row is not None

    def mark_deployment_seen(self, deployment_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO deployments_seen(deployment_id, seen_at)
                VALUES (?, ?)
                """,
                (deployment_id, utc_now()),
            )
            connection.commit()

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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO deployment_requests(
                    id, telegram_user_id, repository_path, revision,
                    command, summary, status, created_at, expires_at
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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='expired', finished_at=?
                WHERE status='pending' AND expires_at <= ?
                """,
                (utc_now(), utc_now()),
            )
            connection.commit()
            return cursor.rowcount

    def unnotified_deployment_requests(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM deployment_requests
                    WHERE status='pending' AND notified_at IS NULL
                    ORDER BY created_at
                    """
                )
            ]

    def mark_deployment_request_notified(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployment_requests SET notified_at=?
                WHERE id=? AND notified_at IS NULL
                """,
                (utc_now(), request_id),
            )
            connection.commit()

    def find_deployment_request(
        self, user_id: int, prefix: str, statuses: tuple[str, ...]
    ) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM deployment_requests
                WHERE telegram_user_id=? AND id LIKE ?
                    AND status IN ({placeholders})
                """,
                (user_id, f"{prefix}%", *statuses),
            ).fetchall()
            if len(rows) != 1:
                raise LookupError(
                    "deployment request prefix must match exactly one eligible request"
                )
            return dict(rows[0])

    def approve_deployment_request(self, request_id: str) -> bool:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='approved', approved_at=?
                WHERE id=? AND status='pending' AND expires_at > ?
                """,
                (now, request_id, now),
            )
            connection.commit()
            return cursor.rowcount == 1

    def deny_deployment_request(self, request_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='denied', finished_at=?
                WHERE id=? AND status='pending'
                """,
                (utc_now(), request_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def start_deployment_request(self, request_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployment_requests
                SET status='executing', started_at=?
                WHERE id=? AND status='approved'
                """,
                (utc_now(), request_id),
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
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployment_requests
                SET status=?, finished_at=?, exit_code=?, output=?, error=?
                WHERE id=? AND status='executing'
                """,
                (
                    status,
                    utc_now(),
                    exit_code,
                    output,
                    error,
                    request_id,
                ),
            )
            connection.commit()

    def recent_deployment_requests(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
