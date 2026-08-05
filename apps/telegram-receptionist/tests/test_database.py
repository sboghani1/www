from datetime import UTC, datetime, timedelta
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


def test_deployment_request_is_one_shot(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repository.parent),))
    now = datetime.now(UTC)
    request_id = "11111111-1111-4111-8111-111111111111"
    assert database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="a" * 40,
        command="echo deploy",
        summary="Deploy test",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    assert not database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="a" * 40,
        command="echo changed",
        summary="Changed",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    assert database.find_deployment_request(123, "", ("pending",))["id"] == request_id
    assert database.approve_deployment_request(request_id)
    assert not database.approve_deployment_request(request_id)
    assert database.start_deployment_request(request_id)
    assert not database.start_deployment_request(request_id)
    database.finish_deployment_request(
        request_id,
        status="succeeded",
        exit_code=0,
        output="done",
        error=None,
    )
    request = database.find_deployment_request(
        123, request_id[:8], ("succeeded",)
    )
    assert request["command"] == "echo deploy"
    assert request["output"] == "done"


def test_deployment_request_expires_before_approval(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repository.parent),))
    now = datetime.now(UTC)
    request_id = "22222222-2222-4222-8222-222222222222"
    database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="b" * 40,
        command="echo deploy",
        summary="Expired deploy",
        created_at=(now - timedelta(minutes=20)).isoformat(),
        expires_at=(now - timedelta(minutes=5)).isoformat(),
    )
    assert database.expire_deployment_requests() == 1
    assert not database.approve_deployment_request(request_id)


def test_deployment_request_denial_and_ambiguous_prefix(tmp_path: Path) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repository.parent),))
    now = datetime.now(UTC)
    for request_id in (
        "33333333-1111-4111-8111-111111111111",
        "33333333-2222-4222-8222-222222222222",
    ):
        database.import_deployment_request(
            request_id=request_id,
            user_id=123,
            repository_path=str(repository),
            revision="c" * 40,
            command="echo deploy",
            summary="Deploy test",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=15)).isoformat(),
        )
    try:
        database.find_deployment_request(123, "33333333", ("pending",))
    except LookupError:
        pass
    else:
        raise AssertionError("ambiguous deployment prefix was accepted")
    request_id = "33333333-1111-4111-8111-111111111111"
    assert database.deny_deployment_request(request_id)
    assert not database.deny_deployment_request(request_id)
    assert not database.approve_deployment_request(request_id)


def test_run_delivery_state_and_event_heartbeat(tmp_path: Path) -> None:
    repository = tmp_path / "repos"
    repository.mkdir()
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repository),))
    database.ensure_user_state(123, 456)
    session = database.create_session(123)
    run = database.enqueue_run(session["id"], 456, "prompt")

    database.start_run(run["id"], 789)
    started = database.get_run(run["id"])
    assert started["last_event_at"]

    database.add_event(run["id"], 0, "assistant", "assistant", {"type": "assistant"})
    event_run = database.get_run(run["id"])
    assert event_run["last_event_at"] >= started["last_event_at"]

    database.finish_run(
        run["id"],
        status="succeeded",
        exit_code=0,
        final_response="done",
        error=None,
    )
    finished = database.get_run(run["id"])
    assert finished["delivery_status"] == "pending"
    assert finished["delivery_cursor"] == 0
    assert database.pending_delivery_runs()[0]["id"] == run["id"]

    database.set_delivery_cursor(run["id"], 1)
    database.mark_delivery_failed(run["id"], "network")
    failed = database.get_run(run["id"])
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_attempts"] == 1
    assert failed["delivery_cursor"] == 1

    database.mark_delivery_succeeded(run["id"])
    delivered = database.get_run(run["id"])
    assert delivered["delivery_status"] == "delivered"
    assert delivered["delivery_attempts"] == 2
    assert delivered["delivered_at"]
