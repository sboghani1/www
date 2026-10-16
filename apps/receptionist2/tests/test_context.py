from pathlib import Path

from receptionist2.context import bootstrap_prompt, build_context_manifest
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
    assert manifest["repositories"] == ["one"]
    assert "RECEPTIONIST2_CONTEXT_V1" in prompt
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
