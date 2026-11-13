import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from decision_helper.books import BookGame, BookLine, divergence, format_book_stats, parse_odds
from decision_helper.models import FinalGame, timestamp
from test_bait import Chat, bait_bot, show_board, tap


def game(betonline, bovada, hard_rock=None):
    lines = {"BetOnline": BookLine(*betonline), "Bovada": BookLine(*bovada)}
    if hard_rock:
        lines["Hard Rock"] = BookLine(*hard_rock)
    return BookGame("Toronto Raptors", "Philadelphia 76ers", "2026-11-13T00:10:00Z", lines)


def test_a_full_point_at_about_the_same_juice_is_a_divergence() -> None:
    found = divergence(game((-8, -110, -110), (-7, -110, -110), (-7, -112, -108)))
    assert found == {
        "side": "Philadelphia 76ers", "betonline_line": -8, "betonline_price": -110,
        "best_book": "Bovada", "best_line": -7, "best_price": -110, "books": ["Bovada", "Hard Rock"],
    }
    # BetOnline making the road team pricier points at the road team.
    away = divergence(game((-6, -110, -110), (-7.5, -110, -110)))
    assert (away["side"], away["betonline_line"], away["best_line"]) == ("Toronto Raptors", 6, 7.5)


def test_half_points_alternate_numbers_and_mixed_reads_are_not_divergences() -> None:
    assert divergence(game((-7.5, -110, -110), (-7, -110, -110))) is None
    # Same price for a different number: an alternate line, not a disagreement.
    assert divergence(game((-8, -110, -110), (-7, -135, +115))) is None
    # One book says BetOnline leans home, the other says away.
    assert divergence(game((-7, -110, -110), (-6, -110, -110), (-8, -110, -110))) is None
    assert divergence(BookGame("A", "B", "t", {"Bovada": BookLine(-7, -110, -110)})) is None


def odds_payload(start: str, betonline_celtics: float) -> list:
    def book(key, celtics, price=-110):
        return {"key": key, "markets": [{"key": "spreads", "outcomes": [
            {"name": "Boston Celtics", "point": celtics, "price": price},
            {"name": "Los Angeles Lakers", "point": -celtics, "price": price},
        ]}]}

    return [
        {"commence_time": start, "away_team": "Boston Celtics", "home_team": "Los Angeles Lakers",
         "bookmakers": [book("betonlineag", betonline_celtics), book("bovada", -5.5), book("hardrockbet", -5.5)]},
        {"commence_time": start, "away_team": "Utah Jazz", "home_team": "Denver Nuggets",
         "bookmakers": [{"key": "betonlineag", "markets": [{"key": "spreads", "outcomes": [
             {"name": "Utah Jazz", "point": 4, "price": -110}, {"name": "Denver Nuggets", "point": -4, "price": -110}]}]},
                        {"key": "bovada", "markets": [{"key": "spreads", "outcomes": [
             {"name": "Utah Jazz", "point": 2.5, "price": -110}, {"name": "Denver Nuggets", "point": -2.5, "price": -110}]}]}]},
    ]


class FakeOdds:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    async def fetch(self, start, end):
        self.calls.append((start, end))
        return parse_odds(self.payload)

    async def close(self):
        pass


def test_every_game_is_checked_an_hour_before_tip_and_only_baited_divergences_are_sent(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    bot.books.initialize()
    bot._bot = SimpleNamespace(send_message=AsyncMock())
    chat = Chat()
    show_board(bot, chat)
    tap(bot, bot.bait_take, "bait:h:5.5:nba-a", chat)  # Lakers +5.5
    start = timestamp(now + timedelta(minutes=50))
    bot.odds = FakeOdds(odds_payload(start, -6.5))
    asyncio.run(bot.check_books())  # tip is an hour out: due now
    rows = {row["event_id"]: row for row in bot.books.all()}

    assert len(bot.odds.calls) == 1
    celtics, jazz = rows["nba-a"], rows["nba-b"]
    assert (celtics["status"], celtics["side"], celtics["best_line"], celtics["betonline_line"]) == (
        "captured", "Boston Celtics", -5.5, -6.5)
    # The Jazz game diverged too (BetOnline Nuggets -4 vs Bovada -2.5) but
    # isn't baited.
    assert (jazz["side"], jazz["best_line"], jazz["alerted_at"]) == ("Denver Nuggets", -2.5, None)
    chat_id, text = bot._bot.send_message.await_args.args
    assert bot._bot.send_message.await_count == 1
    assert "⚖️ 1h before tip · Boston Celtics @ Los Angeles Lakers: BetOnline is a full point off." in text
    assert "• BetOnline: Celtics -6.5 (-110)" in text and "• Hard Rock: Celtics -5.5 (-110)" in text
    assert "BetOnline leans Celtics; the softer number is Celtics -5.5 (-110) at Bovada." in text
    assert "Your bait: Lakers (against BetOnline's side)." in text

    # A second pass doesn't call the Odds API again.
    asyncio.run(bot.check_books())
    assert len(bot.odds.calls) == 1

    with bot.store.connect() as connection:
        connection.execute("UPDATE book_checks SET next_result_check_at=?", (timestamp(now - timedelta(minutes=1)),))
        connection.execute("UPDATE baits SET status='final', result='loss'")
        connection.commit()
    bot.espn.finals = [
        FinalGame("1", "nba", timestamp(now + timedelta(hours=1)), "Boston Celtics", "Los Angeles Lakers", 110, 103),
        FinalGame("2", "nba", timestamp(now + timedelta(hours=1)), "Utah Jazz", "Denver Nuggets", 100, 104),
    ]
    asyncio.run(bot.sync_book_results())
    rows = {row["event_id"]: row for row in bot.books.all()}
    assert (rows["nba-a"]["result_best"], rows["nba-a"]["result_betonline"]) == ("win", "win")
    assert (rows["nba-b"]["result_best"], rows["nba-b"]["result_betonline"]) == ("win", "push")
    stats = "\n".join(format_book_stats(bot.books.all(), bot.baits.all()))
    assert "• games checked: 2, BetOnline a full point off: 2" in stats
    assert "• BetOnline's side at the softer number: 2-0 (100%)" in stats
    assert "• at BetOnline's own number: 1-0-1 (100%)" in stats
    assert "• your bait against BetOnline's side: 0-1 (0%)" in stats


def test_a_game_missing_from_the_books_is_retried_then_missed_at_tip(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    bot.books.initialize()
    bot._bot = SimpleNamespace(send_message=AsyncMock())
    bot.odds = FakeOdds([])
    asyncio.run(bot.check_books())
    rows = bot.books.all()
    assert {row["status"] for row in rows} == {"pending"}
    assert all(row["next_attempt_at"] for row in rows)
    asyncio.run(bot.check_books())
    assert len(bot.odds.calls) == 1  # retries wait 5 minutes
    with bot.store.connect() as connection:
        connection.execute("UPDATE book_checks SET commence_time=?", (timestamp(now - timedelta(minutes=1)),))
        connection.commit()
    bot._books_scheduled_at = now  # keep the test's tip times
    asyncio.run(bot.check_books())
    assert {row["status"] for row in bot.books.all()} == {"missed"}


def test_without_a_key_nothing_is_called(tmp_path: Path) -> None:
    bot = bait_bot(tmp_path, datetime.now(UTC))
    bot.books.initialize()
    assert bot.odds is None
    asyncio.run(bot.check_books())
    assert bot.books.all() == []


def test_betonline_fills_games_bovada_has_not_posted_until_bovada_does(tmp_path: Path) -> None:
    from decision_helper.bait import format_bait_stats
    from decision_helper.bot import EASTERN
    from decision_helper.models import MarketEvent, MarketQuote
    from test_bait import buttons, nba_slate

    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    unposted = MarketEvent(**{**nba_slate(now)[0].__dict__, "markets": {}})
    bot.lines.events = [unposted, nba_slate(now)[1]]
    bot.espn.started = list(bot.lines.events)
    betonline_payload = [{
        "id": "odds-1", "commence_time": timestamp(now + timedelta(hours=1, minutes=10)),
        "away_team": "Boston Celtics", "home_team": "Los Angeles Lakers",
        "bookmakers": [{"key": "betonlineag", "markets": [
            {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "point": -7, "price": -105},
                                            {"name": "Los Angeles Lakers", "point": 7, "price": -115}]},
            {"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": -280},
                                        {"name": "Los Angeles Lakers", "price": 230}]}]}],
    }]
    odds = FakeOdds([])
    odds.betonline_calls = 0

    async def fetch_betonline(start, end):
        odds.betonline_calls += 1
        return betonline_payload

    odds.fetch_betonline = fetch_betonline
    bot.odds = odds
    chat = Chat()

    # Nothing is read until a scheduled slot: the board has no BetOnline yet.
    assert "no line yet" in show_board(bot, chat).text
    assert odds.betonline_calls == 0
    asyncio.run(bot.refresh_betonline())  # the latest slot was missed: read now
    asyncio.run(bot.refresh_betonline())  # once per slot
    assert odds.betonline_calls == 1
    # A restart reuses the saved read instead of spending credits again.
    bot._betonline = None
    asyncio.run(bot.refresh_betonline())
    assert odds.betonline_calls == 1

    board = show_board(bot, chat)
    assert "Boston Celtics @ Los Angeles Lakers: Celtics -7 / Lakers +7 · BetOnline" in board.text
    assert buttons(board.reply_markup)["Celtics -7"] == "bait:ab:-7:nba-a"
    tap(bot, bot.bait_take, "bait:ab:-7:nba-a", chat)
    assert chat.sent[-1].text.startswith("🎣 Taking the bait: Celtics -7 (-105) @ BetOnline")
    bait = bot.baits.latest("nba-a")
    assert (bait["book"], bait["line"], bait["price"]) == ("BetOnline", -7, -105)
    assert odds.betonline_calls == 1  # taps and boards never read BetOnline

    # Bovada posts: its number replaces BetOnline's, and the move says so.
    bot.lines.events = nba_slate(now, celtics_spread=-7.5)
    bot.espn.started = list(bot.lines.events)
    tap(bot, bot.bait_latest, "baitline:nba-a", chat)
    latest = chat.sent[-1]
    assert "📈 Line moved: you took Celtics -7 (-105) @ BetOnline at " in latest.text
    assert "now Celtics -7.5 (-110) @ Bovada." in latest.text
    assert buttons(latest.reply_markup)["🎣 Celtics -7.5"] == "baitre:a:-7.5:nba-a"
    tap(bot, bot.bait_take, "baitre:a:-7.5:nba-a", chat)
    assert bot.baits.latest("nba-a")["book"] == "Bovada"
    board = show_board(bot, chat)
    assert "· BetOnline" not in board.text.split("Celtics @")[1].split("\n")[0]

    with bot.store.connect() as connection:
        connection.execute("UPDATE baits SET status='final', result='win' WHERE book='BetOnline'")
        connection.execute("UPDATE baits SET status='final', result='loss' WHERE book='Bovada'")
        connection.commit()
    report = format_bait_stats(bot.baits.all(), EASTERN)
    assert "Book you took it at" in report
    assert "• BetOnline: 1-0 (100%)" in report and "• Bovada: 0-1 (0%)" in report


def test_without_a_key_unposted_games_stay_without_buttons(tmp_path: Path) -> None:
    from decision_helper.models import MarketEvent
    from test_bait import buttons, nba_slate

    now = datetime.now(UTC)
    bot = bait_bot(tmp_path, now)
    bot.lines.events = [MarketEvent(**{**nba_slate(now)[0].__dict__, "markets": {}})]
    bot.espn.started = list(bot.lines.events)
    board = show_board(bot, Chat())
    assert "Boston Celtics @ Los Angeles Lakers: no line yet" in board.text
    assert buttons(board.reply_markup) == {}


def test_betonline_is_read_at_midnight_and_3pm_pacific() -> None:
    from zoneinfo import ZoneInfo

    from decision_helper.bot import betonline_slot

    pacific = ZoneInfo("America/Los_Angeles")
    at = lambda *args: datetime(2026, 11, *args, tzinfo=pacific).astimezone(UTC)
    assert betonline_slot(at(12, 16, 50)) == at(12, 15, 0)
    assert betonline_slot(at(12, 14, 59)) == at(12, 0, 0)
    assert betonline_slot(at(13, 0, 5)) == at(13, 0, 0)
    assert betonline_slot(at(13, 0, 0)) == at(13, 0, 0)
