import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from decision_helper.bot import DecisionBot, format_review, format_stats
from decision_helper.config import Config
from decision_helper.models import (
    DecisionReview,
    FinalGame,
    extraction_from_payload,
    timestamp,
)
from decision_helper.store import DecisionStore
from helpers import extraction_payload, make_events


class FakeTranscriber:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def transcribe(self, path: Path) -> str:
        assert path.read_bytes() == b"OggS-fake-voice"
        self.paths.append(path)
        return "Bills plus three, I'm 65 percent. Lean under. Celtics win, 70%."

    def warm(self) -> None:
        pass


class FakeClaude:
    def __init__(self) -> None:
        self.reviews: list[dict] = []

    async def extract(self, transcript, events):
        return extraction_from_payload(extraction_payload(), events)

    async def review(self, record):
        self.reviews.append(record)
        return DecisionReview(4, "Covered comfortably", "Specific edge", "Quantify weather", ("overconfidence",))


class FakeOdds:
    def __init__(self, events) -> None:
        self.events = events
        self.calls: list[str] = []

    async def fetch(self, sport):
        self.calls.append(sport)
        return [event for event in self.events if event.sport == sport]

    async def close(self):
        pass


class FakeEspn:
    def __init__(self, finals) -> None:
        self.finals = finals

    async def fetch_finals(self, sport, date):
        return [game for game in self.finals if game.sport == sport]

    async def close(self):
        pass


def make_bot(tmp_path: Path, events, finals=()) -> DecisionBot:
    config = Config(
        bot_token="t",
        allowed_user_id=1,
        expected_username="decision_helpers_bot",
        state_dir=tmp_path / "state",
        odds_api_key="k",
        claude_binary="/usr/bin/claude",
        claude_model="m",
        claude_effort="medium",
        claude_timeout_seconds=30,
        whisper_model="small.en",
        max_download_bytes=1024,
        sync_interval_seconds=60,
    )
    store = DecisionStore(config.database_path)
    store.initialize()
    config.audio_dir.mkdir(parents=True)
    return DecisionBot(
        config,
        store,
        transcriber=FakeTranscriber(),
        claude=FakeClaude(),
        odds=FakeOdds(events),
        espn=FakeEspn(list(finals)),
    )


def voice_update(message_id: int = 7):
    async def download_to_drive(custom_path):
        Path(custom_path).write_bytes(b"OggS-fake-voice")

    telegram_file = SimpleNamespace(download_to_drive=download_to_drive)
    voice = SimpleNamespace(
        mime_type="audio/ogg",
        duration=12,
        file_size=15,
        get_file=AsyncMock(return_value=telegram_file),
    )
    message = SimpleNamespace(
        message_id=message_id,
        voice=voice,
        audio=None,
        document=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=1),
        message=message,
    )


def run_voice(bot: DecisionBot, update) -> None:
    async def scenario() -> None:
        tasks = []
        application = SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.ensure_future(coro)))
        await bot.receive_audio(update, SimpleNamespace(application=application))
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_voice_note_is_transcribed_saved_and_deleted(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    update = voice_update()

    run_voice(bot, update)

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert replies[0].startswith("🎧")
    assert "Saved 3 pick(s)" in replies[1]
    assert "Buffalo Bills +3" in replies[1]
    assert "Under 47.5 [market line]" in replies[1]
    assert "Good specificity" in replies[1]
    assert not any(bot.config.audio_dir.iterdir())
    assert bot.odds.calls == ["nfl", "nba"]

    # Telegram redelivery of the same message is ignored.
    run_voice(bot, update)
    assert len(bot.store.recent()) == 3


def test_failures_are_reported_and_audio_removed(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, [])
    update = voice_update()

    run_voice(bot, update)

    assert "couldn't save" in update.message.reply_text.await_args_list[-1].args[0]
    assert not any(bot.config.audio_dir.iterdir())


def test_unauthorized_users_are_ignored(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    update = voice_update()
    update.effective_user.id = 999
    handler = AsyncMock()
    asyncio.run(bot.authorized(handler)(update, SimpleNamespace()))
    handler.assert_not_awaited()


def test_market_tracking_settlement_and_post_game_review(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events = make_events(now)
    bot = make_bot(tmp_path, events)
    run_voice(bot, voice_update())

    # Line moves: Bills close +1.5, total closes 45.5.
    from decision_helper.models import MarketEvent, MarketQuote

    moved = MarketEvent(
        **{
            **events[0].__dict__,
            "markets": {
                **events[0].markets,
                "spread:Buffalo Bills": MarketQuote(1.5, -110, 5),
                "total:Under": MarketQuote(45.5, -110, 5),
            },
        }
    )
    bot.odds.events = [moved, events[1]]
    with bot.store.connect() as connection:
        connection.execute("UPDATE picks SET next_market_check_at=?", (timestamp(now),))
        connection.commit()
    asyncio.run(bot.sync_markets())

    nfl_final = FinalGame("401", "nfl", events[0].commence_time, "Buffalo Bills", "Kansas City Chiefs", 24, 26)
    nba_final = FinalGame("402", "nba", events[1].commence_time, "Boston Celtics", "Los Angeles Lakers", 99, 101)
    bot.espn.finals = [nfl_final, nba_final]
    with bot.store.connect() as connection:
        connection.execute("UPDATE picks SET next_result_check_at=?", (timestamp(now - timedelta(minutes=1)),))
        connection.commit()
    asyncio.run(bot.sync_results())

    sent = []

    class FakeTelegram:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    asyncio.run(bot.sync_reviews(FakeTelegram()))

    picks = {(p["pick_type"], p["selection"]): p for p in bot.store.recent()}
    spread = picks[("spread", "Buffalo Bills")]
    assert spread["result"] == "win"  # lost by 2 with +3
    assert spread["closing_value"] == 1.5
    under = picks[("total", "Under")]
    assert under["result"] == "loss"  # 50 > 47.5
    assert under["closing_value"] == 2.0  # took Under 47.5, closed 45.5
    assert picks[("winner", "Boston Celtics")]["result"] == "loss"
    assert len(sent) == 3 and all(chat_id == 1 for chat_id, _ in sent)
    assert "Process score: 4/5" in sent[0][1]
    assert bot.claude.reviews[0]["full_transcript"].startswith("Bills plus three")

    stats = bot.store.stats()
    assert (stats["wins"], stats["losses"]) == (1, 2)
    # (0.65-1)^2 and (0.7-0)^2 averaged.
    assert round(stats["brier"], 4) == round(((0.35**2) + (0.7**2)) / 2, 4)
    assert stats["biases"] == [("overconfidence", 3)]
    report = format_stats(stats)
    assert "Record: 1-2-0" in report and "Beat the closing line" in report
    assert "Bias flags: overconfidence" in format_review(spread)


def test_review_failure_is_retried_later(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events = make_events(now)
    bot = make_bot(tmp_path, events)
    run_voice(bot, voice_update())
    bot.espn.finals = [FinalGame("401", "nfl", events[0].commence_time, "Buffalo Bills", "Kansas City Chiefs", 30, 10)]
    with bot.store.connect() as connection:
        connection.execute("UPDATE picks SET next_result_check_at=?", (timestamp(now - timedelta(minutes=1)),))
        connection.commit()
    asyncio.run(bot.sync_results())

    async def failing_review(record):
        raise RuntimeError("usage limit")

    bot.claude.review = failing_review
    asyncio.run(bot.sync_reviews(SimpleNamespace(send_message=AsyncMock())))
    assert bot.store.due_analysis() == []
    with bot.store.connect() as connection:
        rows = connection.execute("SELECT analysis_status, analysis_attempts FROM picks WHERE status='final'").fetchall()
    assert {tuple(row) for row in rows} == {("pending", 1)}


def test_void_open_pick(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    run_voice(bot, voice_update())
    pick = bot.store.recent()[0]
    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    asyncio.run(bot.void(update, SimpleNamespace(args=[pick["id"][:8]])))
    assert "Voided" in reply.await_args.args[0]
    assert bot.store.stats()["total"] == 2


def test_service_uses_claude_subscription_account() -> None:
    service = (Path(__file__).parents[1] / "deploy" / "decision-helper-bot.service").read_text()
    assert "User=receptionist-agent" in service
    assert "Environment=HOME=/home/receptionist-agent" in service
    assert "ReadWritePaths=/home/receptionist-agent/.claude " in service
    assert "UnsetEnvironment=ANTHROPIC_API_KEY" in service
    assert "EnvironmentFile=-/home/receptionist-agent/.claude/auth.env" in service
    installer = (Path(__file__).parents[1] / "deploy" / "install-decision-helper-bot").read_text()
    assert "auth status" in installer and "Claude subscription call OK" in installer
    assert "decision_helpers_bot" in installer
