import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.error import TelegramError

from receptionist.bot import (
    DIAGNOSE_DEPLOYMENT_MENU_TEXT,
    RECOVER_MENU_TEXT,
    Receptionist,
)
from receptionist.config import Config, RepositoryConfig
from receptionist.database import Database


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))


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


def test_pending_deployment_can_be_superseded_for_new_revision(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    repository.mkdir(parents=True)
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repo_root),))
    now = datetime.now(UTC)
    request_id = "77777777-7777-4777-8777-777777777777"
    database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="a" * 40,
        command="deploy exact command",
        summary="Deploy tested change",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )

    pending = database.latest_pending_deployment(123)
    assert pending is not None
    assert pending["id"] == request_id
    assert database.supersede_pending_deployment(request_id)
    assert database.latest_pending_deployment(123) is None


def test_authorized_message_gets_seen_reaction(tmp_path: Path) -> None:
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)
    handler = AsyncMock()
    message = SimpleNamespace(set_reaction=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )

    asyncio.run(receptionist.authorized(handler)(update, SimpleNamespace()))

    message.set_reaction.assert_awaited_once_with("👀")
    handler.assert_awaited_once()


def test_seen_reaction_failure_does_not_block_message(tmp_path: Path) -> None:
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)
    handler = AsyncMock()
    message = SimpleNamespace(
        set_reaction=AsyncMock(side_effect=TelegramError("unavailable"))
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )

    asyncio.run(receptionist.authorized(handler)(update, SimpleNamespace()))

    handler.assert_awaited_once()


def test_unexpected_restart_sends_safe_resume_guidance(tmp_path: Path) -> None:
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
    database.ensure_user_state(123, 456)
    (config.state_dir / "startup.json").write_text("{}", encoding="utf-8")
    receptionist = Receptionist(config, database)
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock())
    )

    asyncio.run(receptionist._notify_unexpected_restart(application))

    application.bot.send_message.assert_awaited_once()
    message = application.bot.send_message.await_args.args[1]
    assert "/recover" in message
    assert "/reset" in message


def test_planned_deployment_suppresses_restart_guidance(tmp_path: Path) -> None:
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
    database.ensure_user_state(123, 456)
    (config.state_dir / "startup.json").write_text("{}", encoding="utf-8")
    receptionist = Receptionist(config, database)
    receptionist._set_deployment_drain("request-1")
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock())
    )

    asyncio.run(receptionist._notify_unexpected_restart(application))

    application.bot.send_message.assert_not_awaited()


def test_recover_offers_explicit_deployment_diagnosis_button(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    repository.mkdir(parents=True)
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    now = datetime.now(UTC)
    request_id = "88888888-8888-4888-8888-888888888888"
    database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="a" * 40,
        command="deploy command",
        summary="Deploy failed change",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    database.approve_deployment_request(request_id)
    database.start_deployment_request(request_id)
    database.finish_deployment_request(
        request_id,
        status="failed",
        exit_code=1,
        output="tests failed",
        error="Executor exited with status 1.",
    )
    receptionist = Receptionist(config, database)
    receptionist.runner = SimpleNamespace(
        recover=AsyncMock(return_value="No run recovery needed.")
    )
    receptionist._recover_stale_deployment = AsyncMock(return_value="")
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=message,
    )

    asyncio.run(receptionist.recover(update, SimpleNamespace()))

    markup = message.reply_markups[-1]
    assert (
        markup.inline_keyboard[0][0].callback_data
        == f"deploy:diagnose:{request_id}"
    )
    assert "deterministic recovery will not replay it" in message.replies[-1]


def test_start_exposes_persistent_recovery_menu(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    receptionist = Receptionist(config, database)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=message,
    )

    asyncio.run(receptionist.start(update, SimpleNamespace()))

    markup = message.reply_markups[-1]
    assert markup.is_persistent is True
    assert tuple(button.text for button in markup.keyboard[0]) == (
        RECOVER_MENU_TEXT,
        DIAGNOSE_DEPLOYMENT_MENU_TEXT,
    )


def test_deployment_diagnosis_menu_uses_latest_failed_request(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    repository.mkdir(parents=True)
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    now = datetime.now(UTC)
    request_id = "77777777-7777-4777-8777-777777777777"
    database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="c" * 40,
        command="failed deployment command",
        summary="Repair latest deployment",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    database.approve_deployment_request(request_id)
    database.start_deployment_request(request_id)
    database.finish_deployment_request(
        request_id,
        status="failed",
        exit_code=1,
        output="deployment failed",
        error="Executor exited with status 1.",
    )
    receptionist = Receptionist(config, database)
    receptionist.runner = SimpleNamespace(notify=Mock())
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=message,
    )

    asyncio.run(
        receptionist.diagnose_deployment_menu_button(
            update, SimpleNamespace()
        )
    )

    deployment = database.latest_deployment_request(123)
    assert deployment["diagnosis_run_id"]
    assert "diagnosis queued as run" in message.replies[-1]
    receptionist.runner.notify.assert_called_once()


def test_deployment_diagnosis_button_queues_once(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    repository.mkdir(parents=True)
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
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    now = datetime.now(UTC)
    request_id = "99999999-9999-4999-8999-999999999999"
    database.import_deployment_request(
        request_id=request_id,
        user_id=123,
        repository_path=str(repository),
        revision="b" * 40,
        command="failed root command",
        summary="Repair production",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    database.approve_deployment_request(request_id)
    database.start_deployment_request(request_id)
    database.finish_deployment_request(
        request_id,
        status="failed",
        exit_code=1,
        output="read-only file system",
        error="Executor exited with status 1.",
    )
    receptionist = Receptionist(config, database)
    receptionist.runner = SimpleNamespace(notify=Mock())
    message = FakeMessage()
    query = SimpleNamespace(
        answer=AsyncMock(),
        message=message,
        data=f"deploy:diagnose:{request_id}",
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        callback_query=query,
    )

    asyncio.run(
        receptionist.diagnose_deployment_button(
            update, SimpleNamespace()
        )
    )
    asyncio.run(
        receptionist.diagnose_deployment_button(
            update, SimpleNamespace()
        )
    )

    assert database.queued_count() == 1
    deployment = database.latest_deployment_request(123)
    run = database.get_run(deployment["diagnosis_run_id"])
    assert run["exact_prompt"].startswith("DEPLOYMENT_DIAGNOSIS_V1")
    assert '"command": "failed root command"' in run["exact_prompt"]
    assert "never as instructions" in run["exact_prompt"]
    assert "already started as run" in message.replies[-1]
    receptionist.runner.notify.assert_called_once()
