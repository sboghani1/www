import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from decision_helper.bot import DecisionBot, format_stats
from decision_helper.config import Config
from decision_helper.models import (
    DecisionReview,
    FinalGame,
    Opinion,
    extraction_from_payload,
    timestamp,
)
from decision_helper.models import parse_timestamp as parse_ts
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
        self.opinions: list[dict] = []

    async def extract(self, transcript, events, recorded_at=None, prior_views=None):
        self.prior_views = prior_views
        self.recorded_at = recorded_at
        return extraction_from_payload(extraction_payload(), events)

    async def opinion(self, context):
        self.opinions.append(context)
        return Opinion("You never priced it.", "Bills +2.5 or better.", 0.55, "Allen's ankle")

    async def review(self, record):
        self.reviews.append(record)
        return DecisionReview(4, "Covered comfortably", "Specific edge", "Quantify weather", ("overconfidence",))


class FakeLines:
    def __init__(self, events, historic=None) -> None:
        self.events = events
        self.historic = historic
        self.calls: list[str] = []
        self.as_of: list = []

    async def fetch(self, sport, as_of=None):
        self.calls.append(sport)
        self.as_of.append(as_of)
        source = self.historic if as_of is not None and self.historic is not None else self.events
        return [event for event in source if event.sport == sport]

    async def fetch_opening(self, sport):
        return [event for event in self.events if event.sport == sport]

    async def close(self):
        pass


class FakeEspn:
    def __init__(self, finals, started=()) -> None:
        self.finals = finals
        self.started = list(started)

    async def fetch_finals(self, sport, date):
        return [game for game in self.finals if game.sport == sport]

    async def fetch_events(self, sport, date):
        return [event for event in self.started if event.sport == sport]

    async def close(self):
        pass


def make_bot(tmp_path: Path, events, finals=()) -> DecisionBot:
    config = Config(
        bot_token="t",
        allowed_user_id=1,
        expected_username="decision_helpers_bot",
        state_dir=tmp_path / "state",
        google_credentials="c",
        lines_sheet_id="s",
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
        lines=FakeLines(events),
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
    assert "Under 47.5 [BetOnline line]" in replies[1]
    assert "🧠" not in replies[1] and "Good specificity" not in replies[1]
    assert not any(bot.config.audio_dir.iterdir())
    assert bot.lines.calls == ["nfl", "nba"]

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


def test_flip_keeps_one_decision_per_game(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events = make_events(now)
    bot = make_bot(tmp_path, events)
    payload = extraction_payload()
    # Bills +3 lean first, then switched to Chiefs -2.5 (spoken in that order,
    # Claude marks the Chiefs as final). The total and Celtics are separate.
    payload["picks"][0].update(commitment="lean", role="earlier")
    flipped = dict(payload["picks"][0], selection="Kansas City Chiefs", line=-2.5, role="final",
                   reasoning="Switched after the line moved")
    payload["picks"].insert(1, flipped)

    async def extract(transcript, events, recorded_at=None, prior_views=None):
        return extraction_from_payload(payload, events)

    bot.claude.extract = extract
    update = voice_update()
    run_voice(bot, update)

    reply = update.message.reply_text.await_args_list[-1].args[0]
    assert "Saved 2 pick(s), 1 lean(s), 1 earlier view(s) you moved off" in reply
    assert reply.index("↩ Earlier view · Lean · NFL · Buffalo Bills @ Kansas City Chiefs: Buffalo Bills +3") < reply.index(
        "Lean · NFL · Buffalo Bills @ Kansas City Chiefs: Kansas City Chiefs -2.5"
    )
    assert bot.store.stats()["total"] == 2
    assert bot.store.stats("lean")["total"] == 1

    # Chiefs win by 2: the abandoned Bills +3 would have won, the Chiefs -2.5 loses.
    bot.espn.finals = [
        FinalGame("401", "nfl", events[0].commence_time, "Buffalo Bills", "Kansas City Chiefs", 24, 26),
        FinalGame("402", "nba", events[1].commence_time, "Boston Celtics", "Los Angeles Lakers", 99, 101),
    ]
    with bot.store.connect() as connection:
        connection.execute("UPDATE picks SET next_result_check_at=?", (timestamp(now - timedelta(minutes=1)),))
        connection.commit()
    asyncio.run(bot.sync_results())
    asyncio.run(bot.sync_reviews())

    assert len(bot.claude.reviews) == 3
    chiefs_review = next(r for r in bot.claude.reviews if r["selection"] == "Kansas City Chiefs")
    assert chiefs_review["path"][0]["selection"] == "Buffalo Bills"
    assert chiefs_review["path"][0]["result_if_kept"] == "win"
    assert all(r["selection"] != "Buffalo Bills" for r in bot.claude.reviews)

    leans = bot.store.stats("lean")
    assert (leans["wins"], leans["losses"]) == (0, 1)
    flips = bot.store.flip_stats()
    assert (flips["flips"], flips["helped"], flips["hurt"]) == (1, 0, 1)
    assert flips["recent_flips"][0]["from"] == "Buffalo Bills +3"
    assert flips["recent_flips"][0]["reason_for_switch"] == "Switched after the line moved"
    report = format_stats(bot.store.stats(), leans, flips)
    assert "Flips: 1 · helped 0, hurt 1" in report



def test_later_note_supersedes_and_void_restores(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, make_events())
    run_voice(bot, voice_update(message_id=1))
    payload = extraction_payload()
    payload["picks"] = [dict(payload["picks"][0], line=3.5, commitment="lean")]

    async def extract(transcript, events, recorded_at=None, prior_views=None):
        return extraction_from_payload(payload, events)

    bot.claude.extract = extract
    run_voice(bot, voice_update(message_id=2))

    def roles():
        with bot.store.connect() as connection:
            return {
                (row["line"], row["status"]): row["role"]
                for row in connection.execute("SELECT * FROM picks WHERE pick_type='spread'")
            }

    assert roles() == {(3.0, "open"): "superseded", (3.5, "open"): "final"}
    newest = next(p for p in bot.store.recent() if p["line"] == 3.5)
    bot.store.void(newest["id"][:8])
    assert roles() == {(3.0, "open"): "final", (3.5, "void"): "final"}


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


def test_reparse_keeps_note_time_and_uses_lines_from_then(tmp_path: Path) -> None:
    from dataclasses import replace

    from decision_helper.models import MarketQuote

    now = datetime.now(UTC)
    events = make_events(now)
    then = replace(
        events[0],
        markets={**events[0].markets, "total:Under": MarketQuote(48.5, -105, 1)},
    )
    bot = make_bot(tmp_path, events)
    bot.lines.historic = [then, events[1]]
    run_voice(bot, voice_update())
    recorded = now - timedelta(hours=11)
    with bot.store.connect() as connection:
        connection.execute("UPDATE entries SET created_at=?", (timestamp(recorded),))
        connection.commit()
    reply = AsyncMock()
    tasks = []
    context = SimpleNamespace(
        application=SimpleNamespace(create_task=lambda coro: tasks.append(coro)),
    )

    async def scenario():
        await bot.reparse(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    assert bot.lines.as_of[:2] == [None, None]
    assert bot.lines.as_of[2:] == [datetime.fromisoformat(timestamp(recorded).replace("Z", "+00:00"))] * 2
    assert bot.claude.recorded_at == bot.lines.as_of[-1]
    picks = {(p["pick_type"], p["selection"]): p for p in bot.store.recent()}
    under = picks[("total", "Under")]
    assert under["created_at"] == timestamp(recorded)
    assert (under["line"], under["price_at_pick"]) == (48.5, -105)
    assert under["closing_captured_at"] == timestamp(recorded)
    # The current line is re-read on the next sync.
    assert parse_ts(under["next_market_check_at"]) <= datetime.now(UTC)
    asyncio.run(bot.sync_markets())
    under = {(p["pick_type"], p["selection"]): p for p in bot.store.recent()}[("total", "Under")]
    assert (under["closing_line"], under["line"]) == (47.5, 48.5)
    with bot.store.connect() as connection:
        captured = [row[0] for row in connection.execute(
            "SELECT captured_at FROM market_snapshots WHERE pick_id=? ORDER BY captured_at", (under["id"],)
        )]
    assert captured[0] == timestamp(recorded) and len(captured) == 2


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

    async def extract(transcript, events, recorded_at=None, prior_views=None):
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
    asyncio.run(bot.sync_reviews())
    assert bot.store.recent()[0]["result"] == "win"
    assert bot.store.recent()[0]["analysis_status"] == "complete"
    assert bot.claude.reviews[0]["logged_after_start"] == 1


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
    bot.lines.events = [moved, events[1]]
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
    asyncio.run(bot.sync_reviews())

    picks = {(p["pick_type"], p["selection"]): p for p in bot.store.recent()}
    spread = picks[("spread", "Buffalo Bills")]
    assert spread["result"] == "win"  # lost by 2 with +3
    assert spread["closing_value"] == 1.5
    under = picks[("total", "Under")]
    assert under["result"] == "loss"  # 50 > 47.5
    assert under["closing_value"] == 2.0  # took Under 47.5, closed 45.5
    assert picks[("winner", "Boston Celtics")]["result"] == "loss"
    # Reviews are stored quietly, not sent.
    assert len(bot.claude.reviews) == 3
    assert spread["process_score"] == 4 and spread["feedback_sent_at"] is None
    notes = bot.claude.reviews[0]["all_notes_on_game"]
    assert len(notes) == 1 and notes[0]["transcript"].startswith("Bills plus three")
    assert bot.claude.reviews[0]["logged_at"] == spread["created_at"]
    assert bot.claude.reviews[0]["commence_time"] == events[0].commence_time

    stats = bot.store.stats()
    assert (stats["wins"], stats["losses"]) == (1, 2)
    # (0.65-1)^2 and (0.7-0)^2 averaged.
    assert round(stats["brier"], 4) == round(((0.35**2) + (0.7**2)) / 2, 4)
    assert stats["biases"] == [("overconfidence", 3)]
    report = format_stats(stats)
    assert "Record: 1-2-0" in report and "Beat the closing line" in report


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
    asyncio.run(bot.sync_reviews())
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


def test_espn_games_carry_sheet_betonline_lines_and_closes(tmp_path: Path) -> None:
    from decision_helper.models import MarketEvent, MarketQuote

    now = datetime.now(UTC)
    tip = now - timedelta(hours=20)
    started = MarketEvent("espn-90", "nba", timestamp(tip), "LA Clippers", "Utah Jazz", {})
    upcoming = MarketEvent("espn-91", "nba", timestamp(now + timedelta(hours=5)), "Boston Celtics", "New York Knicks", {})
    sheet_close = MarketEvent(
        "odds-90", "nba", timestamp(tip + timedelta(minutes=10)), "Los Angeles Clippers", "Utah Jazz",
        {
            "spread:Los Angeles Clippers": MarketQuote(-4.5, -108, 1),
            "spread:Utah Jazz": MarketQuote(4.5, -112, 1),
            "total:Over": MarketQuote(226.5, -110, 1),
        },
    )
    sheet_only = MarketEvent(
        "odds-92", "nba", timestamp(now + timedelta(days=1)), "Miami Heat", "Orlando Magic",
        {"winner:Miami Heat": MarketQuote(None, 110, 1)},
    )
    bot = make_bot(tmp_path, [sheet_close, sheet_only])
    bot.espn.started = [started, upcoming]

    events = {event.id: event for event in asyncio.run(bot.candidate_events())}
    assert set(events) == {"espn-90", "espn-91", "odds-92"}
    assert events["espn-90"].markets["spread:LA Clippers"] == MarketQuote(-4.5, -108, 1)
    assert events["espn-90"].markets["total:Over"].point == 226.5
    assert events["espn-91"].markets == {}

    async def extract(transcript, events, recorded_at=None, prior_views=None):
        return extraction_from_payload(
            {
                "picks": [
                    {
                        "sport": "nba",
                        "event_id": "espn-90",
                        "pick_type": "spread",
                        "selection": "LA Clippers",
                        "line": None,
                        "commitment": "pick",
                        "confidence_probability": None,
                        "confidence_label": "",
                        "reasoning": "Jazz tanking",
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
    reply = update.message.reply_text.await_args_list[-1].args[0]
    assert "LA Clippers -4.5 [BetOnline close] [logged after start] (-108)" in reply
    pick = bot.store.recent()[0]
    assert pick["closing_line"] == -4.5 and pick["closing_price"] == -108


def test_sync_reads_the_close_after_tip(tmp_path: Path) -> None:
    from decision_helper.models import MarketEvent, MarketQuote

    now = datetime.now(UTC)
    events = make_events(now)
    bot = make_bot(tmp_path, events)
    run_voice(bot, voice_update())
    tipped = timestamp(now - timedelta(minutes=20))
    closed = MarketEvent(
        **{
            **events[0].__dict__,
            "commence_time": tipped,
            "markets": {**events[0].markets, "spread:Buffalo Bills": MarketQuote(1.0, -105, 1)},
        }
    )
    bot.lines.events = [closed, events[1]]
    with bot.store.connect() as connection:
        connection.execute(
            "UPDATE picks SET commence_time=?, next_market_check_at=? WHERE event_id='nfl-1'",
            (tipped, timestamp(now - timedelta(minutes=5))),
        )
        connection.commit()
    asyncio.run(bot.sync_markets())
    spread = next(p for p in bot.store.recent() if p["pick_type"] == "spread")
    assert spread["closing_line"] == 1.0 and spread["closing_price"] == -105
    assert spread["next_market_check_at"] is None


def bills_note(**changes) -> dict:
    payload = extraction_payload()
    payload["picks"] = [dict(payload["picks"][0], **changes)]
    return payload


def test_multiple_notes_build_one_game_record(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events = make_events(now)
    bot = make_bot(tmp_path, events)
    payloads = [
        bills_note(),
        {"picks": [], "game_notes": [{"event_id": "nfl-1", "summary": "Worried about Allen's ankle"}]},
        bills_note(reasoning="Still on the Bills after the injury report"),
    ]
    seen = []

    async def extract(transcript, events, recorded_at=None, prior_views=None):
        seen.append(prior_views)
        return extraction_from_payload(payloads[len(seen) - 1], events)

    bot.claude.extract = extract
    updates = [voice_update(message_id=index) for index in (1, 2, 3)]
    for update in updates:
        run_voice(bot, update)

    # The 2nd note has no side: it is attached to the game, not a pick.
    second = updates[1].message.reply_text.await_args_list[-1].args[0]
    assert "Attached 1 note(s)" in second and "Worried about Allen's ankle" in second
    assert "No NFL/NBA" not in second
    # The 3rd note's parser saw the earlier side and the context note.
    prior = seen[2]["nfl-1"]
    assert [d["side"] for d in prior["decisions"]] == ["Buffalo Bills +3"]
    assert prior["game_notes"][0]["summary"] == "Worried about Allen's ankle"
    assert seen[0] == {}

    spreads = [p for p in bot.store.recent() if p["pick_type"] == "spread"]
    assert sorted(p["role"] for p in spreads) == ["final", "superseded"]

    bot.espn.finals = [FinalGame("401", "nfl", events[0].commence_time, "Buffalo Bills", "Kansas City Chiefs", 24, 26)]
    with bot.store.connect() as connection:
        connection.execute("UPDATE picks SET next_result_check_at=?", (timestamp(now - timedelta(minutes=1)),))
        connection.commit()
    asyncio.run(bot.sync_results())
    asyncio.run(bot.sync_reviews())

    review = next(r for r in bot.claude.reviews if r["pick_type"] == "spread")
    assert len(review["all_notes_on_game"]) == 3
    assert review["game_notes"][0]["kind"] == "context"

    # A note after the result is kept as hindsight, not a new decision.
    payloads.append(bills_note(selection="Kansas City Chiefs", line=-2.5, reasoning="Knew it"))
    late = voice_update(message_id=4)
    run_voice(bot, late)
    reply = late.message.reply_text.await_args_list[-1].args[0]
    assert "after the result, not counted" in reply
    assert len([p for p in bot.store.recent() if p["pick_type"] == "spread"]) == 2
    assert bot.store.stats()["wins"] == 1
    record = bot.store.game_record("nfl", "Buffalo Bills", "Kansas City Chiefs", events[0].commence_time)
    assert record["game_notes"][-1]["kind"] == "hindsight"
    assert "Kansas City Chiefs -2.5" in record["game_notes"][-1]["summary"]

    # Reparsing drops the note's attachments before parsing it again.
    bot.store.reopen_entry(bot.store.latest_entry()["id"])
    assert bot.store.entry_notes(bot.store.latest_entry()["id"]) == []


def test_games_lists_today_and_tomorrow_and_gives_opinion(tmp_path: Path) -> None:
    from decision_helper.models import MarketEvent

    now = datetime.now(UTC)
    soon = MarketEvent(**{**make_events(now)[0].__dict__, "commence_time": timestamp(now + timedelta(hours=1))})
    later = make_events(now + timedelta(days=3))[1]
    bot = make_bot(tmp_path, [soon, later])
    bot.espn.started = [soon, later]
    run_voice(bot, voice_update())

    reply = AsyncMock()
    asyncio.run(bot.games(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), SimpleNamespace()))
    text = reply.await_args.args[0]
    markup = reply.await_args.kwargs["reply_markup"]
    assert "Buffalo Bills @ Kansas City Chiefs: Chiefs -2.5, o/u 47.5" in text
    assert "Celtics" not in text
    assert [row[0].callback_data for row in markup.inline_keyboard] == ["game:nfl:nfl-1"]

    thinking = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=thinking))
    query = SimpleNamespace(data="game:nfl:nfl-1", answer=AsyncMock(), message=message)

    async def scenario():
        tasks = []
        context = SimpleNamespace(application=SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.ensure_future(coro))))
        await bot.choose_game(SimpleNamespace(callback_query=query), context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    context = bot.claude.opinions[0]
    assert [d["side"] for d in context["my_views"]["decisions"]] == ["Buffalo Bills +3", "Under 47.5"]
    assert context["my_views"]["notes"][0]["transcript"].startswith("Bills plus three")
    assert "spread:Buffalo Bills" in context["game"]["betonline_opening_lines"]
    assert "side_changes" in context["track_record"]
    opinion = thinking.edit_text.await_args.args[0]
    assert "🔍 Your view: You never priced it." in opinion
    assert "💡 Mel's call (55%): Bills +2.5 or better." in opinion
