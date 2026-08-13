import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from receptionist.bot import Receptionist
from receptionist.config import Config, RepositoryConfig
from receptionist.database import Database
from receptionist.session_policy import (
    assess_rollover,
    extract_topic_terms,
    usage_context_tokens,
)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))


def _config(tmp_path: Path) -> Config:
    repo_root = tmp_path / "repos"
    repo_root.mkdir(exist_ok=True)
    return Config(
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


def _database_with_session(tmp_path: Path) -> tuple[Database, dict]:
    config = _config(tmp_path)
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    session = database.create_session(123)
    database.update_provider_session(session["id"], "claude-session")
    for prompt in (
        "inspect telegram receptionist session context handling",
        "add receptionist session rollover confirmation buttons",
        "test receptionist topic tracking database behavior",
    ):
        run = database.enqueue_run(session["id"], 456, prompt)
        database.finish_run(
            run["id"],
            status="succeeded",
            exit_code=0,
            final_response="done",
            error=None,
        )
    return database, database.get_session(session["id"])


def test_topic_terms_ignore_common_request_language() -> None:
    assert extract_topic_terms(
        "Could you please inspect the Telegram receptionist database?"
    ) == ["inspect", "telegram", "receptionist", "database"]


def test_balanced_policy_suggests_only_clear_topic_change() -> None:
    recent = [
        ["telegram", "receptionist", "session", "context"],
        ["rollover", "confirmation", "buttons", "database"],
    ]
    assert assess_rollover(
        "analyze basketball betting odds and market movement",
        recent,
        successful_runs=3,
        context_tokens=None,
        context_window_tokens=None,
    )
    assert (
        assess_rollover(
            "can you add another button for that",
            recent,
            successful_runs=3,
            context_tokens=None,
            context_window_tokens=None,
        )
        is None
    )


def test_context_usage_includes_cached_input_tokens() -> None:
    tokens, window = usage_context_tokens(
        {
            "message": {
                "usage": {
                    "input_tokens": 2_000,
                    "cache_read_input_tokens": 148_000,
                    "cache_creation_input_tokens": 10_000,
                }
            },
            "contextWindow": 200_000,
        }
    )
    assert tokens == 160_000
    assert window == 200_000


def test_database_tracks_topics_and_latest_context(tmp_path: Path) -> None:
    database, session = _database_with_session(tmp_path)
    assert database.session_rollover_reason(
        session["id"],
        "analyze basketball betting odds and market movement",
    )

    run = database.enqueue_run(session["id"], 456, "continue receptionist work")
    database.add_event(
        run["id"],
        0,
        "assistant",
        "assistant",
        {
            "message": {
                "usage": {
                    "input_tokens": 20_000,
                    "cache_read_input_tokens": 145_000,
                }
            }
        },
    )
    assert "165,000 input tokens" in str(
        database.session_rollover_reason(session["id"], "short follow up")
    )


def test_rollover_suggestion_waits_for_user_choice(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database = Database(config.database_path)
    database.initialize(config.repositories)
    database.ensure_user_state(123, 456)
    session = database.create_session(123)
    database.update_provider_session(session["id"], "claude-session")
    for prompt in (
        "inspect telegram receptionist session context handling",
        "add receptionist session rollover confirmation buttons",
        "test receptionist topic tracking database behavior",
    ):
        run = database.enqueue_run(session["id"], 456, prompt)
        database.finish_run(
            run["id"],
            status="succeeded",
            exit_code=0,
            final_response="done",
            error=None,
        )

    receptionist = Receptionist(config, database)
    receptionist.runner = SimpleNamespace(notify=Mock())
    message = FakeMessage()
    asyncio.run(
        receptionist._enqueue_prompt(
            message,
            user_id=123,
            chat_id=456,
            prompt="analyze basketball betting odds and market movement",
            acknowledgement="received",
        )
    )

    assert database.queued_count() == 0
    markup = message.reply_markups[-1]
    continue_data = markup.inline_keyboard[0][1].callback_data
    assert continue_data.startswith("session:continue:")

    query = SimpleNamespace(
        data=continue_data,
        message=message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
    )
    asyncio.run(
        receptionist.session_rollover_button(update, SimpleNamespace())
    )

    assert database.queued_count() == 1
    assert database.get_user_state(123)["active_session_id"] == session["id"]
    receptionist.runner.notify.assert_called_once()


def test_start_fresh_button_creates_new_session(tmp_path: Path) -> None:
    config = _config(tmp_path)
    database, session = _database_with_session(tmp_path)
    receptionist = Receptionist(config, database)
    receptionist.runner = SimpleNamespace(notify=Mock())
    pending = database.create_pending_rollover(
        user_id=123,
        session_id=session["id"],
        chat_id=456,
        prompt="analyze basketball betting odds and market movement",
        acknowledgement="received",
        reason="New topic.",
    )
    message = FakeMessage()
    query = SimpleNamespace(
        data=f"session:new:{pending['id']}",
        message=message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
    )

    asyncio.run(
        receptionist.session_rollover_button(update, SimpleNamespace())
    )

    state = database.get_user_state(123)
    assert state["active_session_id"] != session["id"]
    assert database.queued_count() == 1
