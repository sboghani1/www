from pathlib import Path

from receptionist.config import RepositoryConfig
from receptionist.database import Database


def test_exact_prompt_is_persisted_unchanged(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("www", repository),))
    database.ensure_user_state(123, 456)
    session = database.create_session(123)
    prompt = 'yes\n\n"keep this exactly"\n```python\nprint("x")\n```'
    run = database.enqueue_run(session["id"], 456, prompt)
    assert database.get_run(run["id"])["exact_prompt"] == prompt


def test_reset_creates_new_provider_context(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("www", repository),))
    database.ensure_user_state(123, 456)
    first = database.create_session(123)
    database.update_provider_session(first["id"], "claude-session")
    second = database.reset_session(123)
    assert first["id"] != second["id"]
    assert second["provider_session_id"] is None


def test_deployment_seen_is_recorded_after_notification(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("www", repository),))
    assert not database.deployment_is_seen("deploy-1")
    database.mark_deployment_seen("deploy-1")
    assert database.deployment_is_seen("deploy-1")


def test_workspace_migration_resets_disabled_repository_selection(
    tmp_path: Path,
) -> None:
    old_repository = tmp_path / "repos" / "old"
    workspace = tmp_path / "repos"
    old_repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("old", old_repository),))
    database.ensure_user_state(123, 456)
    database.create_session(123)

    database.initialize((RepositoryConfig("workspace", workspace),))

    state = database.get_user_state(123)
    assert state["repository_name"] == "workspace"
    assert state["active_session_id"] is None
