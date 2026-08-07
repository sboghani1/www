import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wnba_poller.guesser_bot import (
    GuesserConfig,
    WnbaGuesserBot,
    callback_data,
    game_browser,
    game_detail,
    market_summary,
    period_summary,
    select_games,
    selected_market_context,
    selection_metadata,
)
from wnba_poller.sheets import SheetsStore

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)


def _game(event_id: str = "odds-1", *, days: int = 1) -> dict:
    game = {
        "event_id": event_id,
        "espn_event_id": "espn-1",
        "commence_time_utc": (
            NOW + timedelta(days=days)
        ).isoformat().replace("+00:00", "Z"),
        "commence_time_et": "2026-08-06T12:00:00-04:00",
        "away_team": "Atlanta Dream",
        "home_team": "New York Liberty",
        "bookmaker": "BetOnline.ag",
        "opening_captured_at": "2026-08-05T14:00:00Z",
        "latest_captured_at": "2026-08-05T15:45:00Z",
    }
    values = {
        "away_spread": 4.5,
        "away_spread_price": -110,
        "away_moneyline": 150,
        "home_spread": -4.5,
        "home_spread_price": -110,
        "home_moneyline": -175,
        "total": 164.5,
        "over_price": -105,
        "under_price": -115,
        "first_half_away_spread": 2.5,
        "first_half_away_spread_price": -112,
        "first_half_home_spread": -2.5,
        "first_half_home_spread_price": -108,
        "first_half_total": 82.5,
        "first_half_over_price": -110,
        "first_half_under_price": -110,
    }
    for snapshot in ("opening", "latest"):
        for field, value in values.items():
            game[f"{snapshot}_{field}"] = value
    return game


def test_game_browser_uses_14_day_window_and_bounded_callbacks() -> None:
    records = [
        _game("first", days=1),
        _game("boundary", days=14),
        _game("outside", days=15),
    ]

    selected = select_games(records, now=NOW)
    text, keyboard = game_browser(records, page=0, now=NOW)

    assert [game["event_id"] for game in selected] == [
        "first",
        "boundary",
    ]
    assert "next 14 days" in text
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "g:0:first" in callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
    assert len(callback_data("g", f"0:{'x' * 48}").encode("utf-8")) <= 64


def test_game_flow_has_full_and_first_half_market_choices() -> None:
    game = _game()

    detail, detail_keyboard = game_detail(game, page=0)
    full_text, full_keyboard = period_summary(game, period="game")
    half_text, half_keyboard = period_summary(
        game, period="first_half"
    )

    assert "Full game" in detail
    assert "First half" in detail
    assert "Moneyline" in detail
    assert [
        button.callback_data
        for button in detail_keyboard.inline_keyboard[0]
    ] == ["p:game", "p:first_half"]
    assert "Moneyline" in full_text
    assert [
        button.callback_data
        for button in full_keyboard.inline_keyboard[0]
    ] == ["m:spread", "m:moneyline", "m:total"]
    assert "First half" in half_text
    assert [
        button.callback_data
        for button in half_keyboard.inline_keyboard[0]
    ] == ["m:spread", "m:total"]


def test_market_side_context_and_sheet_metadata() -> None:
    game = _game()

    text, keyboard = market_summary(
        game,
        period="first_half",
        market="total",
    )
    context = selected_market_context(
        game,
        period="first_half",
        market="total",
        side="under",
    )
    metadata = selection_metadata(
        game,
        period="first_half",
        market="total",
        side="under",
    )

    assert "Opening" in text and "Latest" in text
    assert [
        button.callback_data
        for button in keyboard.inline_keyboard[0]
    ] == ["s:over", "s:under"]
    assert context == {
        "opening_line": 82.5,
        "opening_price": -110,
        "latest_line": 82.5,
        "latest_price": -110,
    }
    assert metadata["period"] == "first_half"
    assert metadata["market"] == "total"
    assert metadata["side"] == "Under"


def test_guesser_config_requires_fixed_bot_identity(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_GUESSER_BOT_TOKEN", "token")
    monkeypatch.setenv(
        "WNBA_GUESSER_EXPECTED_USERNAME", "wnbaguesser_bot"
    )

    config = GuesserConfig.from_env()

    assert config.expected_username == "wnbaguesser_bot"
    monkeypatch.setenv(
        "WNBA_GUESSER_EXPECTED_USERNAME", "wrong_bot"
    )
    with pytest.raises(ValueError):
        GuesserConfig.from_env()


def test_sheet_allowlist_is_numeric_and_enabled_only() -> None:
    store = SheetsStore(None)
    store.read_allowed_users = lambda: [
        {"telegram_user_id": "123", "enabled": "true"},
        {"telegram_user_id": "456", "enabled": "false"},
        {"telegram_user_id": "not-a-number", "enabled": "true"},
    ]

    assert store.allowed_user_ids() == {123}


class FakeStore:
    def __init__(self) -> None:
        self.game = _game()
        self.append_calls: list[dict] = []
        self.fail = False
        self.game_reads = 0

    def read_games(self) -> list[dict]:
        self.game_reads += 1
        return [self.game]

    def allowed_user_ids(self) -> set[int]:
        return {123}

    def read_recent_thoughts(self, *, limit: int) -> list[dict]:
        assert limit == 10
        return [
            {
                "submitted_at_et": "2026-08-05T12:00:00-04:00",
                "away_team": "Atlanta Dream",
                "home_team": "New York Liberty",
                "period": "first_half",
                "market": "total",
                "side": "Under",
                "thought_text": "=exact recent thought",
            }
        ]

    def append_thought_record(self, **kwargs):
        self.append_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("sheet failed")
        return (
            len(self.append_calls) == 1,
            {
                "submitted_at_et": "2026-08-05T12:00:00-04:00",
                "away_team": "Atlanta Dream",
                "home_team": "New York Liberty",
                "thought_text": kwargs["thought_text"],
            },
        )


class FakeMessage:
    def __init__(
        self,
        text: str,
        *,
        message_id: int,
        prompt_id: int,
    ) -> None:
        self.text = text
        self.message_id = message_id
        self.reply_to_message = SimpleNamespace(message_id=prompt_id)
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=999)


def _update(message: FakeMessage):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123, username="guesser"),
        effective_chat=SimpleNamespace(id=123, type="private"),
        message=message,
    )


def _state() -> dict:
    return {
        "event_id": "odds-1",
        "game": _game(),
        "page": 0,
        "period": "first_half",
        "market": "total",
        "side": "under",
        "prompt_message_id": 100,
        "expires_at": datetime.now(UTC) + timedelta(days=1),
        "completed_message_ids": set(),
    }


def _bot(store: FakeStore) -> WnbaGuesserBot:
    return WnbaGuesserBot(
        config=GuesserConfig(
            bot_token="token",
            expected_username="wnbaguesser_bot",
            sheet_timeout_seconds=5,
        ),
        store=store,
    )


def test_exact_reasoning_is_appended_and_repeat_button_is_offered() -> None:
    store = FakeStore()
    bot = _bot(store)
    bot.states[123] = _state()
    message = FakeMessage(
        '=exact\n"reasoning"',
        message_id=456,
        prompt_id=100,
    )

    asyncio.run(
        bot.capture_reasoning(_update(message), SimpleNamespace())
    )

    call = store.append_calls[0]
    assert call["thought_id"] == "telegram:123:456"
    assert call["thought_text"] == '=exact\n"reasoning"'
    assert call["selection_metadata"]["period"] == "first_half"
    assert call["selection_metadata"]["side"] == "Under"
    callbacks = [
        button.callback_data
        for row in message.replies[0][1].inline_keyboard
        for button in row
    ]
    assert callbacks == ["repeat", "games:0", "hist"]
    assert bot.states[123]["period"] == "first_half"
    assert "prompt_message_id" not in bot.states[123]

    bot.states[123]["prompt_message_id"] = 101
    second = FakeMessage(
        "updated reasoning",
        message_id=457,
        prompt_id=101,
    )
    asyncio.run(
        bot.capture_reasoning(_update(second), SimpleNamespace())
    )
    assert [call["thought_id"] for call in store.append_calls] == [
        "telegram:123:456",
        "telegram:123:457",
    ]
    assert store.append_calls[-1]["thought_text"] == "updated reasoning"


def test_failed_write_retries_original_submission_id() -> None:
    store = FakeStore()
    store.fail = True
    bot = _bot(store)
    bot.states[123] = _state()
    first = FakeMessage("same exact text", message_id=456, prompt_id=100)

    asyncio.run(
        bot.capture_reasoning(_update(first), SimpleNamespace())
    )

    assert bot.states[123]["attempt_message_id"] == 456
    store.fail = False
    retry = FakeMessage("same exact text", message_id=457, prompt_id=100)
    asyncio.run(
        bot.capture_reasoning(_update(retry), SimpleNamespace())
    )

    assert store.append_calls[-1]["thought_id"] == "telegram:123:456"
    assert store.append_calls[-1]["thought_text"] == "same exact text"
    assert bot.states[123]["completed_message_ids"] == {456, 457}


def test_unauthorized_user_is_rejected_before_game_access() -> None:
    store = FakeStore()
    bot = _bot(store)
    called = False

    async def handler(update, context):
        nonlocal called
        called = True

    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=999, type="private"),
        message=message,
        callback_query=None,
    )

    asyncio.run(
        bot.authorized(handler)(update, SimpleNamespace())
    )

    assert not called
    assert store.game_reads == 0
    message.reply_text.assert_awaited_once_with("Not authorized.")


def test_bot_startup_requires_expected_identity_and_seeded_allowlist() -> None:
    store = FakeStore()
    bot = _bot(store)
    application = SimpleNamespace(
        bot=SimpleNamespace(
            get_me=AsyncMock(
                return_value=SimpleNamespace(username="wnbaguesser_bot")
            ),
            set_my_commands=AsyncMock(),
        )
    )

    asyncio.run(bot.post_init(application))

    assert bot._allowed_ids == {123}
    application.bot.set_my_commands.assert_awaited_once()

    store.allowed_user_ids = lambda: set()
    empty = _bot(store)
    with pytest.raises(RuntimeError, match="must be seeded"):
        asyncio.run(empty.post_init(application))


def test_recent_thoughts_and_cancel_flow() -> None:
    store = FakeStore()
    bot = _bot(store)
    bot.states[123] = _state()
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
        message=message,
    )

    asyncio.run(bot.recent_thoughts(update, SimpleNamespace()))

    recent_text = message.reply_text.await_args_list[0].args[0]
    assert "First half · Total · Under" in recent_text
    assert "=exact recent thought" in recent_text

    asyncio.run(bot.cancel(update, SimpleNamespace()))

    assert 123 not in bot.states
    assert message.reply_text.await_args_list[-1].args[0] == (
        "WNBA guess cancelled."
    )


def test_dedicated_service_uses_separate_env_and_fixed_entrypoint() -> None:
    service = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "wnba-guesser-bot.service"
    ).read_text(encoding="utf-8")

    assert "User=receptionist-agent" in service
    assert (
        "EnvironmentFile=/home/receptionist-agent/.config/wnba-guesser/env"
        in service
    )
    assert "ExecStart=/opt/wnba-poller/current/bin/wnba-guesser-bot" in service
    assert "telegram-receptionist" not in service
