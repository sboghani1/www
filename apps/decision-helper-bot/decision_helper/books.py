"""BetOnline vs Bovada and Hard Rock, 60 minutes before every NBA tip.

The idea being tested: when BetOnline's spread is a full point away from a
recreational book's at similar juice, BetOnline's side (the one it prices
more expensively) wins more often. Every NBA game gets one three-book
snapshot from The Odds API an hour before tip (spreads only: one credit per
call, and a call that returns no games is free). Every snapshot is graded;
Mel only messages about divergences on games you baited.

This is the one place Mel spends Odds API credits. Everything else reads the
forwarder's Bovada sheet.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .models import (
    FinalGame,
    MarketEvent,
    MarketQuote,
    grade_pick,
    implied_probability,
    parse_timestamp,
    team_matches,
    timestamp,
    utc_now,
)
from .store import DecisionStore


log = logging.getLogger("decision_helper.books")
ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
BOOKS = {"betonlineag": "BetOnline", "bovada": "Bovada", "hardrockbet": "Hard Rock"}
CHECK_BEFORE = timedelta(minutes=60)
RETRY_EVERY = timedelta(minutes=5)  # a game missing from the books (pulled)
SCHEDULE_EVERY = timedelta(minutes=30)
SPREAD_GAP = 1.0
# "About the same juice": within 2.5 points of implied probability (about
# 10 cents around -110). A bigger juice gap can make a different number the
# same price (an alternate line), so it doesn't count.
JUICE_GAP = 0.025
RESULT_DELAY = timedelta(minutes=150)


@dataclass(frozen=True)
class BookLine:
    home_spread: float
    home_price: int
    away_price: int


@dataclass(frozen=True)
class BookGame:
    away_team: str
    home_team: str
    commence_time: str
    lines: dict[str, BookLine]  # by book name


def parse_odds(payload: Any) -> list[BookGame]:
    """Odds API ``/odds`` events with each book's full-game spread."""
    games = []
    for event in payload if isinstance(payload, list) else []:
        try:
            away, home = event["away_team"], event["home_team"]
            lines: dict[str, BookLine] = {}
            for book in event.get("bookmakers") or []:
                name = BOOKS.get(book.get("key"))
                market = next(
                    (m for m in book.get("markets") or [] if m.get("key") == "spreads"),
                    None,
                )
                if name is None or market is None:
                    continue
                prices = {o["name"]: o for o in market.get("outcomes") or []}
                if home not in prices or away not in prices:
                    continue
                lines[name] = BookLine(
                    float(prices[home]["point"]),
                    int(prices[home]["price"]),
                    int(prices[away]["price"]),
                )
            games.append(BookGame(away, home, str(event["commence_time"]), lines))
        except (KeyError, TypeError, ValueError):
            continue
    return games


def parse_betonline(payload: Any) -> list[MarketEvent]:
    """BetOnline spreads and moneylines from an Odds API ``/odds`` reply, as
    MarketEvents for the bait board's fallback."""
    events = []
    for event in payload if isinstance(payload, list) else []:
        try:
            book = next(
                b for b in event.get("bookmakers") or [] if b.get("key") == "betonlineag"
            )
            markets: dict[str, MarketQuote] = {}
            for market in book.get("markets") or []:
                kind = {"spreads": "spread", "h2h": "winner"}.get(market.get("key"))
                if kind is None:
                    continue
                for outcome in market.get("outcomes") or []:
                    point = outcome.get("point") if kind == "spread" else None
                    markets[f"{kind}:{outcome['name']}"] = MarketQuote(
                        float(point) if point is not None else None,
                        int(outcome["price"]),
                        1,
                    )
            events.append(
                MarketEvent(
                    id=str(event["id"]),
                    sport="nba",
                    commence_time=str(event["commence_time"]),
                    away_team=event["away_team"],
                    home_team=event["home_team"],
                    markets=markets,
                    book="BetOnline",
                )
            )
        except (KeyError, StopIteration, TypeError, ValueError):
            continue
    return events


def divergence(game: BookGame) -> dict[str, Any] | None:
    """BetOnline's side when its spread is at least a full point off Bovada
    or Hard Rock at about the same juice; None when it isn't, or when the two
    books disagree about which way BetOnline leans."""
    betonline = game.lines.get("BetOnline")
    if betonline is None:
        return None
    compared = []
    for name in ("Bovada", "Hard Rock"):
        other = game.lines.get(name)
        if other is None:
            continue
        # Positive: BetOnline makes the home team the bigger favorite.
        gap = other.home_spread - betonline.home_spread
        if abs(gap) < SPREAD_GAP:
            continue
        side_home = gap > 0
        mine = betonline.home_price if side_home else betonline.away_price
        theirs = other.home_price if side_home else other.away_price
        if abs(implied_probability(mine) - implied_probability(theirs)) > JUICE_GAP:
            continue
        sign = 1 if side_home else -1
        compared.append(
            {
                "book": name,
                "side": game.home_team if side_home else game.away_team,
                "line": sign * other.home_spread,
                "price": theirs,
                "betonline_line": sign * betonline.home_spread,
                "betonline_price": mine,
            }
        )
    if not compared or len({item["side"] for item in compared}) != 1:
        return None
    # The softer number is the one you could actually bet.
    best = max(compared, key=lambda item: item["line"])
    return {
        "side": best["side"],
        "betonline_line": best["betonline_line"],
        "betonline_price": best["betonline_price"],
        "best_book": best["book"],
        "best_line": best["line"],
        "best_price": best["price"],
        "books": [item["book"] for item in compared],
    }


class OddsClient:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch(self, start: datetime, end: datetime) -> list[BookGame]:
        """Spreads from the three books for games tipping in [start, end]."""
        response = await self.client.get(
            ODDS_URL,
            params={
                "apiKey": self.api_key,
                "bookmakers": ",".join(BOOKS),
                "markets": "spreads",
                "oddsFormat": "american",
                "commenceTimeFrom": _odds_time(start),
                "commenceTimeTo": _odds_time(end),
            },
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Odds API returned HTTP {response.status_code}")
        log.info(
            "Odds API book check: %s credit(s), %s left",
            response.headers.get("x-requests-last"),
            response.headers.get("x-requests-remaining"),
        )
        return parse_odds(response.json())

    async def fetch_betonline(self, start: datetime, end: datetime) -> list[Any]:
        """The raw Odds API reply with BetOnline spreads and moneylines (2
        credits when any game is listed; parse with ``parse_betonline``) for
        the bait board's games Bovada hasn't posted."""
        response = await self.client.get(
            ODDS_URL,
            params={
                "apiKey": self.api_key,
                "bookmakers": "betonlineag",
                "markets": "spreads,h2h",
                "oddsFormat": "american",
                "commenceTimeFrom": _odds_time(start),
                "commenceTimeTo": _odds_time(end),
            },
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Odds API returned HTTP {response.status_code}")
        log.info(
            "Odds API BetOnline fallback: %s credit(s), %s left",
            response.headers.get("x-requests-last"),
            response.headers.get("x-requests-remaining"),
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []


def _odds_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BookStore:
    """One row per NBA game: its three-book snapshot and grades."""

    def __init__(self, store: DecisionStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS book_checks (
                    event_id TEXT PRIMARY KEY,
                    commence_time TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                        'pending','captured','missed'
                    )),
                    next_attempt_at TEXT,
                    captured_at TEXT,
                    books_json TEXT NOT NULL DEFAULT '{}',
                    -- BetOnline's side and the softer number when it diverged
                    side TEXT,
                    betonline_line REAL,
                    betonline_price INTEGER,
                    best_book TEXT,
                    best_line REAL,
                    best_price INTEGER,
                    alerted_at TEXT,
                    result_best TEXT,
                    result_betonline TEXT,
                    away_score INTEGER,
                    home_score INTEGER,
                    next_result_check_at TEXT,
                    settled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS book_checks_due
                    ON book_checks(status, commence_time);
                """
            )
            connection.commit()

    def schedule(self, events: list[MarketEvent]) -> None:
        """Every NBA game gets a pending check; tip times stay current."""
        with self.store.connect() as connection:
            for event in events:
                if event.sport != "nba":
                    continue
                connection.execute(
                    """
                    INSERT INTO book_checks (event_id, commence_time, away_team, home_team)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET commence_time=excluded.commence_time
                    WHERE book_checks.status='pending'
                    """,
                    (event.id, event.commence_time, event.away_team, event.home_team),
                )
            connection.commit()

    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Pending games within an hour of tip; games that tipped without a
        snapshot are marked missed."""
        now = now or utc_now()
        with self.store.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM book_checks WHERE status='pending'"
                ).fetchall()
            ]
            due = []
            for row in rows:
                commence = parse_timestamp(row["commence_time"])
                if commence <= now:
                    connection.execute(
                        "UPDATE book_checks SET status='missed' WHERE event_id=?",
                        (row["event_id"],),
                    )
                elif commence - CHECK_BEFORE <= now and (
                    row["next_attempt_at"] is None
                    or parse_timestamp(row["next_attempt_at"]) <= now
                ):
                    due.append(row)
            connection.commit()
        return due

    def retry_later(self, event_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE book_checks SET next_attempt_at=? WHERE event_id=?",
                (timestamp(utc_now() + RETRY_EVERY), event_id),
            )
            connection.commit()

    def capture(self, row: dict[str, Any], game: BookGame) -> dict[str, Any]:
        found = divergence(game)
        books = {
            name: {
                "home_spread": line.home_spread,
                "home_price": line.home_price,
                "away_price": line.away_price,
            }
            for name, line in game.lines.items()
        }
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE book_checks SET status='captured', captured_at=?,
                    books_json=?, side=?, betonline_line=?, betonline_price=?,
                    best_book=?, best_line=?, best_price=?,
                    next_result_check_at=?
                WHERE event_id=?
                """,
                (
                    timestamp(utc_now()),
                    json.dumps(books),
                    found and found["side"],
                    found and found["betonline_line"],
                    found and found["betonline_price"],
                    found and found["best_book"],
                    found and found["best_line"],
                    found and found["best_price"],
                    timestamp(parse_timestamp(row["commence_time"]) + RESULT_DELAY),
                    row["event_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM book_checks WHERE event_id=?", (row["event_id"],)
            ).fetchone()
            connection.commit()
        return dict(updated)

    def mark_alerted(self, event_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE book_checks SET alerted_at=? WHERE event_id=?",
                (timestamp(utc_now()), event_id),
            )
            connection.commit()

    def due_results(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM book_checks WHERE status='captured' "
                "AND settled_at IS NULL AND next_result_check_at<=?",
                (timestamp(utc_now()),),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_result(self, event_id: str, minutes: int = 15) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE book_checks SET next_result_check_at=? WHERE event_id=?",
                (timestamp(utc_now() + timedelta(minutes=minutes)), event_id),
            )
            connection.commit()

    def settle(self, row: dict[str, Any], game: FinalGame) -> None:
        def grade(line: float | None) -> str | None:
            if row["side"] is None or line is None:
                return None
            return grade_pick(
                pick_type="spread",
                selection=row["side"],
                line=line,
                away_team=row["away_team"],
                home_team=row["home_team"],
                away_score=game.away_score,
                home_score=game.home_score,
            )

        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE book_checks SET result_best=?, result_betonline=?,
                    away_score=?, home_score=?, settled_at=?
                WHERE event_id=?
                """,
                (
                    grade(row["best_line"]),
                    grade(row["betonline_line"]),
                    game.away_score,
                    game.home_score,
                    timestamp(utc_now()),
                    row["event_id"],
                ),
            )
            connection.commit()

    def all(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM book_checks ORDER BY commence_time"
            ).fetchall()
        return [dict(row) for row in rows]


def match_game(row: dict[str, Any], games: list[BookGame]) -> BookGame | None:
    """The Odds API lists tip 10 minutes after ESPN; match on teams."""
    target = parse_timestamp(row["commence_time"])
    candidates = [
        game
        for game in games
        if team_matches(game.away_team, row["away_team"])
        and team_matches(game.home_team, row["home_team"])
        and abs(parse_timestamp(game.commence_time) - target) <= timedelta(hours=3)
    ]
    return candidates[0] if candidates else None


def _short(team: str) -> str:
    return team.split()[-1]


def _number(team: str, line: float, price: int) -> str:
    number = "pk" if line == 0 else f"{line:+g}"
    return f"{_short(team)} {number} ({price:+d})"


def format_divergence(row: dict[str, Any], bait: dict[str, Any] | None) -> str:
    """The message for a baited game where BetOnline diverged."""
    books = json.loads(row["books_json"])
    side_home = row["side"] == row["home_team"]
    lines = [
        f"⚖️ 1h before tip · {row['away_team']} @ {row['home_team']}: "
        "BetOnline is a full point off.",
    ]
    for name in ("BetOnline", "Bovada", "Hard Rock"):
        line = books.get(name)
        if line is None:
            lines.append(f"• {name}: no line")
            continue
        spread = line["home_spread"] if side_home else -line["home_spread"]
        price = line["home_price"] if side_home else line["away_price"]
        lines.append(f"• {name}: {_number(row['side'], spread, price)}")
    lines.append(
        f"BetOnline leans {_short(row['side'])}; the softer number is "
        f"{_number(row['side'], row['best_line'], row['best_price'])} at "
        f"{row['best_book']}."
    )
    if bait is not None:
        with_it = bait["selection"] == row["side"]
        lines.append(
            f"Your bait: {_short(bait['selection'])}"
            + (" (same side as BetOnline)." if with_it else " (against BetOnline's side).")
        )
    return "\n".join(lines)


def _record(results: Counter) -> str:
    text = f"{results['win']}-{results['loss']}"
    if results["push"]:
        text += f"-{results['push']}"
    decided = results["win"] + results["loss"]
    if decided:
        text += f" ({results['win'] / decided:.0%})"
    return text


def format_book_stats(rows: list[dict[str, Any]], baits: list[dict[str, Any]]) -> list[str]:
    """Lines for /baitstats: how BetOnline's side did when it diverged."""
    captured = [row for row in rows if row["status"] == "captured"]
    if not captured:
        return []
    diverged = [row for row in captured if row["side"]]
    lines = [
        "",
        "BetOnline vs Bovada/Hard Rock (all NBA games, 1h before tip)",
        f"• games checked: {len(captured)}, BetOnline a full point off: {len(diverged)}",
    ]
    settled = [row for row in diverged if row["settled_at"]]
    if settled:
        best = Counter(row["result_best"] for row in settled)
        own = Counter(row["result_betonline"] for row in settled)
        lines.append(f"• BetOnline's side at the softer number: {_record(best)}")
        lines.append(f"• at BetOnline's own number: {_record(own)}")
        final_take: dict[str, dict[str, Any]] = {}
        for bait in sorted(baits, key=lambda item: item["ordinal"]):
            if bait["action"] == "take":
                final_take[bait["event_id"]] = bait
        versus: dict[str, Counter] = {}
        for row in settled:
            bait = final_take.get(row["event_id"])
            if bait is None or bait["result"] not in {"win", "loss", "push"}:
                continue
            label = (
                "your bait with BetOnline's side"
                if bait["selection"] == row["side"]
                else "your bait against BetOnline's side"
            )
            versus.setdefault(label, Counter())[bait["result"]] += 1
        for label, results in sorted(versus.items()):
            lines.append(f"• {label}: {_record(results)}")
    return lines
