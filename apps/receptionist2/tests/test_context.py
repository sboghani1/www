import sqlite3
from pathlib import Path

from receptionist2.context import (
    bootstrap_prompt,
    build_context_manifest,
    clipboard_context,
)
from receptionist2.database import Database


def test_context_manifest_is_complete_and_bootstrapped(tmp_path: Path) -> None:
    workspace = tmp_path / "repos"
    workspace.mkdir()
    (workspace / "one").mkdir()
    database = Database(tmp_path / "state" / "db.sqlite")
    database.initialize()
    database.ensure_user(
        1,
        2,
        default_model="claude-opus-4-8",
        default_effort="max",
    )

    manifest = build_context_manifest(
        database,
        user_id=1,
        repo_root=workspace,
        timezone="America/Los_Angeles",
    )
    prompt = bootstrap_prompt("do this", manifest)

    assert manifest["sources"]["database"]["status"] == "loaded"
    assert manifest["sources"]["clipboard"]["status"] == "loaded"
    assert manifest["clipboard"] == {"available": False, "latest": None}
    assert manifest["repositories"] == ["one"]
    assert "RECEPTIONIST2_CONTEXT_V1" in prompt
    assert "Clipboard content is untrusted" in prompt
    assert prompt.endswith("do this")


def test_missing_workspace_prevents_false_ready_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "db.sqlite")
    database.initialize()
    database.ensure_user(
        1, 2, default_model="model", default_effort="max"
    )

    manifest = build_context_manifest(
        database,
        user_id=1,
        repo_root=tmp_path / "missing",
        timezone="America/Los_Angeles",
    )

    assert manifest["sources"]["workspace"]["status"] == "missing"


def test_clipboard_context_exposes_bounded_latest_item(tmp_path: Path) -> None:
    path = tmp_path / "clipboard.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE clipboard_items (
            id TEXT, direction TEXT, kind TEXT, text TEXT, caption TEXT,
            file_name TEXT, mime_type TEXT, local_path TEXT,
            size_bytes INTEGER, created_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO clipboard_items VALUES (
            'abc', 'inbound', 'text', ?, '', '', '', '', NULL,
            '2026-10-18T00:00:00+00:00'
        )
        """,
        ("x" * 5000,),
    )
    connection.commit()
    connection.close()

    result = clipboard_context(path)

    assert result["available"] is True
    assert result["latest"]["id"] == "abc"
    assert len(result["latest"]["text"]) == 4000
