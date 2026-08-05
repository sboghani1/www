import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from receptionist.bot import Receptionist
from receptionist.config import Config, RepositoryConfig
from receptionist.database import Database


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def test_request_file_is_copied_into_database_and_removed(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    request_dir = repo_root / ".receptionist" / "deploy-requests"
    repository.mkdir(parents=True)
    (repository / ".git").mkdir()
    request_dir.mkdir(parents=True)
    request_id = "44444444-4444-4444-8444-444444444444"
    now = datetime.now(UTC)
    request_path = request_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "repository_path": str(repository),
                "revision": "d" * 40,
                "summary": "Deploy immutable request",
                "command": "echo exact-command",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    config = Config(
        telegram_token="test-token",
        allowed_user_id=123,
        repo_root=repo_root,
        repositories=(RepositoryConfig("workspace", repo_root),),
        state_dir=tmp_path / "state",
        claude_binary="/usr/bin/claude",
        agent_launcher="/launcher",
        agent_killer="/killer",
        deploy_request_dir=request_dir,
        deploy_executor="/executor",
        deploy_timeout_seconds=900,
        wnba_helper="/wnba-helper",
        wnba_helper_timeout_seconds=45,
        agent_timeout_seconds=3600,
        max_queued_messages=10,
        model=None,
    )
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)

    asyncio.run(receptionist._ingest_deployment_requests())

    assert not request_path.exists()
    request = database.find_deployment_request(123, request_id, ("pending",))
    assert request["command"] == "echo exact-command"
    assert request["revision"] == "d" * 40


def test_deployment_drain_blocks_new_prompts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    config = Config(
        telegram_token="test-token",
        allowed_user_id=123,
        repo_root=repo_root,
        repositories=(RepositoryConfig("workspace", repo_root),),
        state_dir=tmp_path / "state",
        claude_binary="/usr/bin/claude",
        agent_launcher="/launcher",
        agent_killer="/killer",
        deploy_request_dir=repo_root / ".receptionist" / "deploy-requests",
        deploy_executor="/executor",
        deploy_timeout_seconds=900,
        wnba_helper="/wnba-helper",
        wnba_helper_timeout_seconds=45,
        agent_timeout_seconds=3600,
        max_queued_messages=10,
        model=None,
    )
    config.state_dir.mkdir()
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)
    receptionist._set_deployment_drain("request-1")
    message = FakeMessage()

    asyncio.run(
        receptionist._enqueue_prompt(
            message,
            user_id=123,
            chat_id=456,
            prompt="must not queue",
            acknowledgement="received",
        )
    )

    assert database.queued_count() == 0
    assert "deployment is still completing" in message.replies[0]


def test_deployment_drain_survives_until_explicitly_cleared(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    config = Config(
        telegram_token="test-token",
        allowed_user_id=123,
        repo_root=repo_root,
        repositories=(RepositoryConfig("workspace", repo_root),),
        state_dir=tmp_path / "state",
        claude_binary="/usr/bin/claude",
        agent_launcher="/launcher",
        agent_killer="/killer",
        deploy_request_dir=repo_root / ".receptionist" / "deploy-requests",
        deploy_executor="/executor",
        deploy_timeout_seconds=900,
        wnba_helper="/wnba-helper",
        wnba_helper_timeout_seconds=45,
        agent_timeout_seconds=3600,
        max_queued_messages=10,
        model=None,
    )
    config.state_dir.mkdir()
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)

    receptionist._set_deployment_drain("request-1")
    assert receptionist._deployment_drain_path.exists()
    receptionist._clear_deployment_drain()
    assert not receptionist._deployment_drain_path.exists()


def test_head_changed_deployment_can_be_cloned_once(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repo_root),))
    now = datetime.now(UTC)
    old_id = "55555555-5555-4555-8555-555555555555"
    database.import_deployment_request(
        request_id=old_id,
        user_id=123,
        repository_path=str(repository),
        revision="a" * 40,
        command="deploy exact command",
        summary="Deploy tested change",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    assert database.approve_deployment_request(old_id)
    assert database.start_deployment_request(old_id)
    database.finish_deployment_request(
        old_id,
        status="failed",
        exit_code=2,
        output="Repository HEAD changed after the deployment request was displayed.",
        error="Executor exited with status 2.",
    )

    failed = database.latest_head_changed_deployment(123)
    assert failed is not None
    new_id = "66666666-6666-4666-8666-666666666666"
    revision = "b" * 40
    assert database.import_deployment_request(
        request_id=new_id,
        user_id=123,
        repository_path=str(repository),
        revision=revision,
        command=failed["command"],
        summary=failed["summary"],
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
        recovered_from_id=old_id,
    )
    equivalent = database.equivalent_pending_deployment(
        user_id=123,
        repository_path=str(repository),
        revision=revision,
        command=failed["command"],
    )
    assert equivalent is not None
    assert equivalent["id"] == new_id
    assert database.latest_head_changed_deployment(123) is None
