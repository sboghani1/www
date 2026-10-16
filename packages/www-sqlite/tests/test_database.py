from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from www_sqlite import Migration, apply_migrations, connect, transaction


def test_connect_configures_database_and_read_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "state" / "app.sqlite3"
    with connect(path) as connection:
        connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
        with transaction(connection):
            connection.execute(
                "INSERT INTO values_table(value) VALUES (?)", ("stored",)
            )
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    with connect(path, read_only=True) as connection:
        assert connection.execute(
            "SELECT value FROM values_table"
        ).fetchone()["value"] == "stored"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO values_table(value) VALUES ('blocked')"
            )


def test_connect_requires_absolute_path(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="absolute"):
        connect(Path("relative.sqlite3"))


def test_apply_migrations_is_idempotent_and_detects_renames(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite3"

    def create_items(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )

    migration = Migration(1, "create-items", create_items)
    with connect(path) as connection:
        apply_migrations(connection, (migration,))
        apply_migrations(connection, (migration,))
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="recorded"):
            apply_migrations(
                connection,
                (Migration(1, "renamed-items", create_items),),
            )


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    with connect(tmp_path / "app.sqlite3") as connection:
        connection.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with pytest.raises(RuntimeError, match="failure"):
            with transaction(connection):
                connection.execute(
                    "INSERT INTO items(value) VALUES ('not committed')"
                )
                raise RuntimeError("failure")
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
