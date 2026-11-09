import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from decision_helper.bait import format_bait_stats
from decision_helper.bot import EASTERN
from decision_helper.models import FinalGame, MarketEvent, MarketQuote, timestamp
from decision_helper.models import parse_timestamp as parse_ts
from helpers import make_events
from test_bot import make_bot


def nba_slate(now: datetime, *, celtics_spread: float = -5.5) -> list[MarketEvent]:
    start = timestamp(now + timedelta(hours=1))
    return [
        MarketEvent(
            id="nba-a",
            sport="nba",
            commence_time=start,
            away_team="Boston Celtics",
            home_team="Los Angeles Lakers",
            markets={
                "spread:Boston Celtics": MarketQuote(celtics_spread, -110, 1),
                "spread:Los Angeles Lakers": MarketQuote(-celtics_spread, -110, 1),
                "winner:Boston Celtics": MarketQuote(None, -220, 1),
                "winner:Los Angeles Lakers": MarketQuote(None, 180, 1),
                "total:Over": MarketQuote(228.5, -110, 1),
                "total:Under": MarketQuote(228.5, -110, 1),
            },
        ),
        MarketEvent(
            id="nba-b",
            sport="nba",
            commence_time=start,
            away_team="Utah Jazz",
            home_team="Denver Nuggets",
            markets={
                "winner:Utah Jazz": MarketQuote(None, 105, 1),
                "winner:Denver Nuggets": MarketQuote(None, -125, 1),
            },
        ),
    ]


def bait_bot(tmp_path: Path, now: datetime):
    events = nba_slate(now) + [make_events(now - timedelta(days=2) + timedelta(hours=1))[0]]
    bot = make_bot(tmp_path, events)
    bot.baits.initialize()
    bot.espn.started = list(events)
    return bot


class Chat:
    """Mel's outgoing messages, each with an id replies can point to."""

    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []

    async def reply_text(self, text, reply_markup=None):
        message = SimpleNamespace(
            message_id=100 + len(self.sent), text=text, reply_markup=reply_markup
        )
        self.sent.append(message)
        return message


def buttons(markup) -> dict[str, str]:
    return {button.text: button.callback_data for row in markup.inline_keyboard for button in row}


def tap(bot, handler, data: str, chat: Chat):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        message=chat,
    )
    tasks = []
    context = SimpleNamespace(application=SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.ensure_future(coro))))

    async def scenario():
        await handler(SimpleNamespace(callback_query=query), context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    return query


def show_board(bot, chat: Chat):
    asyncio.run(bot.bait_board(SimpleNamespace(message=chat), SimpleNamespace()))
    return chat.sent[-1]


def text_reply(bot, text: str, reply_to: int):
    message = SimpleNamespace(
        message_id=900 + reply_to,
        text=text,
        reply_to_message=SimpleNamespace(message_id=reply_to),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=message)
    context = SimpleNamespace(application=SimpleNamespace(create_task=lambda coro: coro.close()))
    asyncio.run(bot.receive_text(update, context))
    return message


def test_board_shows_nba_sides_and_a_tap_logs_the_bait(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    chat = Chat()

    board = show_board(bot, chat)
    assert "Boston Celtics @ Los Angeles Lakers: Celtics -5.5 / Lakers +5.5, o/u 228.5" in board.text
    assert "Utah Jazz @ Denver Nuggets: Jazz pk / Nuggets pk" in board.text
    assert "Bills" not in board.text
    assert buttons(board.reply_markup) == {
        "Celtics -5.5": "bait:a:-5.5:nba-a",
        "Lakers +5.5": "bait:h:5.5:nba-a",
        "Jazz pk": "bait:a:ml:nba-b",
        "Nuggets pk": "bait:h:ml:nba-b",
    }

    query = tap(bot, bot.bait_take, "bait:h:5.5:nba-a", chat)
    # The board is redrawn: the baited game now offers a latest-line check.
    board_text = query.edit_message_text.await_args.args[0]
    board_buttons = buttons(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert "· 🎣 Lakers +5.5 (-110)" in board_text
    assert board_buttons["🔄 Latest line: Celtics @ Lakers"] == "baitline:nba-a"
    assert "Lakers +5.5" not in board_buttons
    confirmation = chat.sent[-1]
    assert confirmation.text.startswith("🎣 Taking the bait: Lakers +5.5 (-110)")
    assert "reply to this message with a reason" in confirmation.text

    # A moneyline-only game is a pick'em.
    tap(bot, bot.bait_take, "bait:a:ml:nba-b", chat)
    jazz = bot.baits.latest("nba-b")
    assert (jazz["pick_type"], jazz["line"], jazz["price"]) == ("winner", None, 105)

    lakers = bot.baits.latest("nba-a")
    assert (lakers["selection"], lakers["line"], lakers["price"], lakers["ordinal"]) == (
        "Los Angeles Lakers", 5.5, -110, 1)
    assert json.loads(lakers["lines_json"])["away_moneyline"] == -220

    # Tapping the same side again doesn't double-count it.
    tap(bot, bot.bait_take, "bait:h:5.5:nba-a", chat)
    assert chat.sent[-1].text.startswith("Already logged")
    assert len(bot.baits.game_history("nba-a")) == 1


def test_tags_and_text_or_voice_replies_become_reasons_not_notes(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:a:-5.5:nba-a", chat)
    confirmation = chat.sent[-1]

    query = tap(bot, bot.bait_tag, buttons(confirmation.reply_markup)["⭐ Star / big name"], chat)
    edited = query.edit_message_text.await_args
    assert "Tags: ⭐ Star / big name" in edited.args[0]
    assert "✅ ⭐ Star / big name" in buttons(edited.kwargs["reply_markup"])

    message = text_reply(bot, "Tatum looked unstoppable last night", confirmation.message_id)
    assert "Reason saved for Celtics -5.5" in message.reply_text.await_args.args[0]

    tap(bot, bot.bait_why, buttons(confirmation.reply_markup)["✍️ Add a reason"], chat)
    prompt = chat.sent[-1]
    assert prompt.text.startswith("✍️ Why does Celtics -5.5 (-110) look so tempting?")
    text_reply(bot, "Lakers on a back-to-back", prompt.message_id)

    async def download_to_drive(custom_path):
        Path(custom_path).write_bytes(b"OggS-fake-voice")

    voice = SimpleNamespace(
        mime_type="audio/ogg", duration=3, file_size=15,
        get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=download_to_drive)),
    )
    voice_message = SimpleNamespace(
        message_id=555, voice=voice, audio=None, document=None,
        reply_to_message=SimpleNamespace(message_id=confirmation.message_id),
        reply_text=AsyncMock(),
    )
    tasks = []
    context = SimpleNamespace(application=SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.ensure_future(coro))))

    async def scenario():
        await bot.receive_audio(SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=voice_message), context)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    bait = bot.baits.latest("nba-a")
    assert json.loads(bait["tags_json"]) == ["star"]
    assert bait["reason"].split("\n") == [
        "Tatum looked unstoppable last night",
        "Lakers on a back-to-back",
        "Bills plus three, I'm 65 percent. Lean under. Celtics win, 70%.",
    ]
    # None of it became a parsed note, and the voice audio is not kept.
    assert bot.store.latest_entry() is None
    assert not any((bot.config.state_dir / "bait-audio").iterdir())


def test_latest_line_offers_another_bite_when_the_number_moves(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:a:-5.5:nba-a", chat)

    tap(bot, bot.bait_latest, "baitline:nba-a", chat)
    assert "No change: still Celtics -5.5 (-110)" in chat.sent[-1].text

    bot.lines.events = nba_slate(now, celtics_spread=-7.5)
    tap(bot, bot.bait_latest, "baitline:nba-a", chat)
    latest = chat.sent[-1]
    assert "📈 Line moved: you took Celtics -5.5 (-110)" in latest.text
    assert "now Celtics -7.5 (-110)" in latest.text
    assert buttons(latest.reply_markup) == {
        "🎣 Celtics -7.5": "baitre:a:-7.5:nba-a",
        "🎣 Lakers +7.5": "baitre:h:7.5:nba-a",
        "🙅 Not tempted anymore": "baitpass:a:-7.5:nba-a",
    }

    query = tap(bot, bot.bait_take, "baitre:a:-7.5:nba-a", chat)
    query.edit_message_reply_markup.assert_awaited_with(reply_markup=None)
    assert chat.sent[-1].text.startswith(
        "🎣 Taking the bait: Celtics -7.5 (-110) (take #2, first was Celtics -5.5 (-110))"
    )

    bot.lines.events = nba_slate(now, celtics_spread=-9)
    tap(bot, bot.bait_latest, "baitline:nba-a", chat)
    tap(bot, bot.bait_take, buttons(chat.sent[-1].reply_markup)["🙅 Not tempted anymore"], chat)
    assert chat.sent[-1].text.startswith("🙅 Not tempted anymore: Celtics -9 (-110)")
    history = bot.baits.game_history("nba-a")
    assert [(row["action"], row["line"], row["ordinal"]) for row in history] == [
        ("take", -5.5, 1), ("take", -7.5, 2), ("pass", -9, 3)]
    assert history[1]["previous_id"] == history[0]["id"]


def test_bait_closes_at_tip_then_is_graded_and_summarized(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:a:-5.5:nba-a", chat)
    tap(bot, bot.bait_tag, f"baittag:star:{bot.baits.latest('nba-a')['id']}", chat)
    bot.lines.events = nba_slate(now, celtics_spread=-7.5)
    tap(bot, bot.bait_take, "baitre:a:-7.5:nba-a", chat)
    tap(bot, bot.bait_take, "bait:h:ml:nba-b", chat)

    # Tip-off: no more bait on the game.
    with bot.store.connect() as connection:
        connection.execute(
            "UPDATE baits SET commence_time=?, next_result_check_at=?",
            (timestamp(now - timedelta(hours=3)), timestamp(now - timedelta(minutes=1))),
        )
        connection.commit()
    tipped = nba_slate(now - timedelta(hours=4), celtics_spread=-8)
    bot.lines.events = tipped
    bot.espn.started = tipped
    query = tap(bot, bot.bait_latest, "baitline:nba-a", chat)
    assert "tipped off" in query.answer.await_args.args[0]

    bot.espn.finals = [
        FinalGame("1", "nba", timestamp(now - timedelta(hours=3)), "Boston Celtics", "Los Angeles Lakers", 110, 104),
        FinalGame("2", "nba", timestamp(now - timedelta(hours=3)), "Utah Jazz", "Denver Nuggets", 99, 120),
    ]
    asyncio.run(bot.sync_baits())

    rows = {(row["event_id"], row["ordinal"]): row for row in bot.baits.all()}
    assert rows[("nba-a", 1)]["result"] == "win"  # won by 6 at -5.5
    assert rows[("nba-a", 2)]["result"] == "loss"  # chased -7.5
    assert rows[("nba-a", 1)]["closing_line"] == -8
    assert rows[("nba-b", 1)]["result"] == "win"

    report = format_bait_stats(bot.baits.all(), EASTERN)
    assert "🎣 Bait report: 3 take(s), 0 waiting on a result" in report
    assert "• all takes: 2-1 (67%)" in report
    assert "• final take per game: 1-1 (50%)" in report
    assert "• re-take, chased a worse number: 0-1 (0%)" in report
    assert "• favorite 3.5-6.5: 1-0 (100%)" in report
    assert "• pick'em: 1-0 (100%)" in report
    assert "• market moved toward your side: 1-1 (50%)" in report
    assert "• ⭐ Star / big name: 1-0 (100%)" in report
    assert "❌" in report and "[re-take]" in report
    assert "|" not in report  # Telegram-friendly bullets, no tables


def test_check_schedule_is_every_3h_then_hourly_then_15_minutes_before_tip() -> None:
    from decision_helper.bait import next_check

    tip = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)
    checks = []
    now = tip - timedelta(hours=12, minutes=5)
    while (due := next_check(timestamp(tip), now)) is not None:
        checks.append(tip - due)
        now = due
    assert checks == [
        timedelta(hours=10), timedelta(hours=7), timedelta(hours=4), timedelta(hours=3),
        timedelta(hours=2), timedelta(hours=1), timedelta(minutes=15),
    ]


def test_scheduled_checks_alert_on_a_full_point_move_since_you_last_saw_it(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    bot._bot = SimpleNamespace(send_message=AsyncMock())
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:a:-5.5:nba-a", chat)
    tap(bot, bot.bait_take, "bait:a:ml:nba-b", chat)
    watch = bot.baits.watch("nba-a")
    # Baited an hour before tip: the next check is 15 minutes before.
    assert parse_ts(watch["next_check_at"]) == parse_ts(bot.baits.latest("nba-a")["commence_time"]) - timedelta(minutes=15)

    def run_check(spread):
        bot.lines.events = nba_slate(now, celtics_spread=spread)
        with bot.store.connect() as connection:
            connection.execute("UPDATE bait_watch SET next_check_at=?", (timestamp(now - timedelta(minutes=1)),))
            connection.commit()
        asyncio.run(bot.check_baited_lines())

    run_check(-6)  # half a point: no alert
    bot._bot.send_message.assert_not_awaited()
    run_check(-6.5)  # a full point
    chat_id, text = bot._bot.send_message.await_args.args
    assert chat_id == 1
    assert "before tip: your bait's line moved" in text
    assert "you took Celtics -5.5 (-110)" in text and "now Celtics -6.5 (-110)" in text
    assert "🙅 Not tempted anymore" in buttons(bot._bot.send_message.await_args.kwargs["reply_markup"])
    assert bot.baits.watch("nba-a")["alert_line"] == -6.5
    run_check(-7)  # half a point past the alert: no repeat
    assert bot._bot.send_message.await_count == 1
    run_check(-7.5)
    assert bot._bot.send_message.await_count == 2
    # Taking the bait again makes its number the new baseline.
    tap(bot, bot.bait_take, "baitre:a:-7.5:nba-a", chat)
    assert bot.baits.watch("nba-a")["alert_line"] is None
    run_check(-8)
    assert bot._bot.send_message.await_count == 2
    # The moneyline-only game stays quiet until a real spread appears.
    assert bot.baits.watch("nba-b")["next_check_at"] is not None


def test_existing_baits_get_a_check_schedule(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:a:-5.5:nba-a", chat)
    with bot.store.connect() as connection:
        connection.execute("DELETE FROM bait_watch")
        connection.commit()
    bot.baits.initialize()
    assert bot.baits.watch("nba-a")["next_check_at"] is not None
