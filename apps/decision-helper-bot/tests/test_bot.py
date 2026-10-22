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
    def __init__(self, finals, started=()) -> None:
        self.finals = finals
        self.started = list(started)

    async def fetch_finals(self, sport, date):
        return [game for game in self.finals if game.sport == sport]

    async def fetch_started(self, sport, date):
        return [event for event in self.started if event.sport == sport]

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


def run_voice(bot: DecisionBot, update, choice: str = "parse") -> None:
    async def scenario() -> None:
        tasks = []
        application = SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.ensure_future(coro)))
        context = SimpleNamespace(application=application)
        before = update.message.reply_text.await_count
        await bot.receive_audio(update, context)
        calls = update.message.reply_text.await_args_list[before:]
        if calls and "reply_markup" in calls[-1].kwargs:
            await tap(bot, update, calls[-1].kwargs["reply_markup"], choice, context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


async def tap(bot, update, markup, choice, context):
    data = {button.text: button.callback_data for button in markup.inline_keyboard[0]}
    button = data["🧠 Parse" if choice == "parse" else "🗑 Trash"]
    query = SimpleNamespace(
        data=button,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=update.message,
    )
    await bot.choose(SimpleNamespace(callback_query=query), context)
    return query


def test_voice_note_waits_for_button_then_parses_and_deletes(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    update = voice_update()

    run_voice(bot, update)

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert replies[0] == "🎧 Got it. Parse this note or trash the audio?"
    assert "Saved 3 pick(s)" in replies[1]
    assert "Buffalo Bills +3" in replies[1]
    assert "Under 47.5 [market line]" in replies[1]
    assert "Good specificity" in replies[1]
    assert not any(bot.config.audio_dir.iterdir())
    assert bot.odds.calls == ["nfl", "nba"]

    # Telegram redelivery of the same message is ignored.
    run_voice(bot, update)
    assert len(bot.store.recent()) == 3


def test_audio_is_kept_until_choice_and_trash_deletes_it(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    update = voice_update()

    async def scenario():
        context = SimpleNamespace(application=SimpleNamespace(create_task=lambda coro: coro.close()))
        await bot.receive_audio(update, context)
        assert [path.read_bytes() for path in bot.config.audio_dir.iterdir()] == [b"OggS-fake-voice"]
        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        query = await tap(bot, update, markup, "trash", context)
        query.edit_message_text.assert_awaited_with("🗑 Trashed. The audio was deleted.")
        again = await tap(bot, update, markup, "parse", context)
        again.edit_message_text.assert_awaited_with("This note was already handled.")

    asyncio.run(scenario())

    assert not any(bot.config.audio_dir.iterdir())
    assert bot.transcriber.paths == []
    assert bot.store.recent() == []
    with bot.store.connect() as connection:
        assert connection.execute("SELECT status FROM entries").fetchone()[0] == "trashed"


def test_leans_are_saved_and_reported_separately(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    payload = extraction_payload()
    payload["picks"][0]["commitment"] = "lean"
    flipped = dict(payload["picks"][0], selection="Kansas City Chiefs", line=-2.5)
    payload["picks"].insert(1, flipped)

    async def extract(transcript, events):
        return extraction_from_payload(payload, events)

    bot.claude.extract = extract
    update = voice_update()
    run_voice(bot, update)

    reply = update.message.reply_text.await_args_list[-1].args[0]
    assert "Saved 2 pick(s) and 2 lean(s)" in reply
    assert reply.index("Lean · NFL · Buffalo Bills @ Kansas City Chiefs: Buffalo Bills +3") < reply.index(
        "Lean · NFL · Buffalo Bills @ Kansas City Chiefs: Kansas City Chiefs -2.5"
    )
    assert bot.store.stats()["total"] == 2
    assert bot.store.stats("lean")["total"] == 2
    report = format_stats(bot.store.stats(), bot.store.stats("lean"))
    assert "Committed picks" in report and "Leans (tracked separately)" in report


def test_reparse_voids_open_picks_and_reuses_transcript(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    run_voice(bot, voice_update())
    reply = AsyncMock()
    tasks = []
    context = SimpleNamespace(
        application=SimpleNamespace(create_task=lambda coro: tasks.append(coro)),
    )

    async def scenario():
        await bot.reparse(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    assert reply.await_args_list[0].args[0] == "🔁 Reparsing your latest note…"
    assert "Saved 3 pick(s)" in reply.await_args_list[1].args[0]
    assert len(bot.transcriber.paths) == 1
    with bot.store.connect() as connection:
        statuses = [row[0] for row in connection.execute("SELECT status FROM picks ORDER BY status")]
    assert statuses == ["open"] * 3 + ["void"] * 3


def test_v1_database_is_migrated(tmp_path: Path) -> None:
    import re
    import sqlite3

    fresh = DecisionStore(tmp_path / "fresh.db")
    fresh.initialize()
    with fresh.connect() as connection:
        schema = {
            row["name"]: row["sql"]
            for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        }
    v1_picks = re.sub(
        r",\s*commitment TEXT.*?logged_after_start INTEGER NOT NULL DEFAULT 0", "", schema["picks"], flags=re.S
    )
    v1_entries = (
        schema["entries"]
        .replace("'awaiting_choice',", "")
        .replace(",\n                        'trashed'", "")
        .replace("audio_suffix TEXT NOT NULL DEFAULT '',", "")
    )
    assert "commitment" not in v1_picks and "awaiting_choice" not in v1_entries

    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.execute(v1_entries)
    connection.execute(v1_picks)
    connection.execute(
        "INSERT INTO entries VALUES ('e1', 1, 14, 'voice', 'Patriots at Bears', '', 'saved', '', "
        "'2026-10-22T06:25:27Z', '2026-10-22T06:25:55Z')"
    )
    connection.commit()
    connection.close()

    store = DecisionStore(path)
    store.initialize()
    store.initialize()

    latest = store.latest_entry()
    assert latest["transcript"] == "Patriots at Bears"
    assert latest["audio_suffix"] == ""
    with store.connect() as connection:
        assert "awaiting_choice" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='entries'"
        ).fetchone()[0]
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(picks)")}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {"commitment", "ordinal", "logged_after_start"} <= columns
    assert store.reopen_entry("e1")["status"] == "processing"


def test_picks_for_started_games_are_logged_and_graded(tmp_path: Path) -> None:
    from decision_helper.models import MarketEvent

    now = datetime.now(UTC)
    tuesday = MarketEvent("espn-77", "nba", timestamp(now - timedelta(hours=26)), "Detroit Pistons", "Chicago Bulls", {})
    bot = make_bot(tmp_path, [])
    bot.espn.started = [tuesday]
    seen = {}

    async def extract(transcript, events):
        seen["events"] = events
        return extraction_from_payload(
            {
                "picks": [
                    {
                        "sport": "nba",
                        "event_id": "espn-77",
                        "pick_type": "spread",
                        "selection": "Chicago Bulls",
                        "line": -4.5,
                        "commitment": "pick",
                        "confidence_probability": 0.6,
                        "confidence_label": "",
                        "reasoning": "Pistons on a back-to-back",
                        "uncertainties": [],
                    }
                ],
                "immediate_feedback": "Logged.",
            },
            events,
        )

    bot.claude.extract = extract
    update = voice_update()
    run_voice(bot, update)

    assert [event.id for event in seen["events"]] == ["espn-77"]
    reply = update.message.reply_text.await_args_list[-1].args[0]
    assert "Chicago Bulls -4.5 [logged after start]" in reply
    pick = bot.store.recent()[0]
    assert pick["logged_after_start"] == 1 and pick["next_market_check_at"] is None

    bot.espn.finals = [FinalGame("77", "nba", tuesday.commence_time, "Detroit Pistons", "Chicago Bulls", 100, 110)]
    asyncio.run(bot.sync_results())
    sent = []

    class FakeTelegram:
        async def send_message(self, chat_id, text):
            sent.append(text)

    asyncio.run(bot.sync_reviews(FakeTelegram()))
    assert bot.store.recent()[0]["result"] == "win"
    assert bot.claude.reviews[0]["logged_after_start"] == 1
    assert sent and sent[0].startswith("🏁 WIN")


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
