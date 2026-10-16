from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Migration versions must be positive")
        if not self.name.strip():
            raise ValueError("Migration names must not be empty")


def connect(
    path: Path,
    *,
    read_only: bool = False,
    timeout_seconds: float = 30,
) -> sqlite3.Connection:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("SQLite database path must be absolute")
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(path)
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=timeout_seconds,
            isolation_level=None,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path,
            timeout=timeout_seconds,
            isolation_level=None,
        )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
    if not read_only:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]:
    if connection.in_transaction:
        raise RuntimeError("Nested SQLite transactions are not supported")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
) -> None:
    ordered = sorted(migrations, key=lambda migration: migration.version)
    versions = [migration.version for migration in ordered]
    if len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row["version"]): str(row["name"])
        for row in connection.execute(
            "SELECT version, name FROM schema_migrations"
        )
    }
    for migration in ordered:
        existing_name = applied.get(migration.version)
        if existing_name is not None:
            if existing_name != migration.name:
                raise RuntimeError(
                    f"Migration {migration.version} is recorded as "
                    f"{existing_name!r}, not {migration.name!r}"
                )
            continue
        with transaction(connection, immediate=True):
            migration.apply(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at_utc)
                VALUES (?, ?, ?)
                """,
                (migration.version, migration.name, utc_now()),
            )
