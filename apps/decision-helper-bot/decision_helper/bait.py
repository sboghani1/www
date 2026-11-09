"""Bait tracker: NBA sides that look too easy to pass up.

``/bait`` lists the day's NBA games with Bovada spreads (the forwarder's
``nba_games`` tab has been filled from Bovada since 2026-10-27). Tapping a side
logs it as bait at the number shown. Checking a baited game's latest line
offers both sides again at the current number (and "not tempted anymore"), so
every re-take is its own row with the line it was taken at. Reasons are
optional: quick tags on the confirmation, and free text or a voice note
replied to it. Baits are graded from ESPN and summarized by ``/baitstats`` so
the kinds of bait that win and lose show up over time. Baits are separate from
picks and leans and never feed reviews or learnings.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .models import (
    FinalGame,
    MarketEvent,
    grade_pick,
    parse_timestamp,
    timestamp,
    utc_now,
)
from .store import DecisionStore


# Quick reasons, stored by key so labels can be reworded later.
BAIT_TAGS = (
    ("star", "⭐ Star / big name"),
    ("cheap", "🤑 Number too cheap"),
    ("hot", "🔥 Hot team"),
    ("cold", "🧊 Opponent is bad"),
    ("spot", "🏠 Home/road spot"),
    ("injury", "🩼 Injury news"),
    ("bounce", "🔁 Bounce-back"),
    ("rest", "💤 Rest/schedule"),
)
TAG_LABELS = dict(BAIT_TAGS)
RESULT_DELAY = timedelta(minutes=150)  # an NBA game lasts about 2.5 hours
CLOSE_DELAY = timedelta(minutes=15)  # the forwarder's last pre-tip poll is in by then
# Scheduled line checks on a baited game: 15 minutes and 1-4 hours before
# tip, and every 3 hours before that (7h, 10h, ...). A check alerts only when
# the line moved a full point from the last number you saw.
CHECK_OFFSETS = (timedelta(minutes=15), *(timedelta(hours=h) for h in range(1, 5)))
EARLY_CHECK_EVERY = timedelta(hours=3)
ALERT_MOVE = 1.0


def next_check(commence_time: str, now: datetime) -> datetime | None:
    """The first scheduled check after ``now``; None once the last
    (15 minutes before tip) has passed."""
    commence = parse_timestamp(commence_time)
    offsets = list(CHECK_OFFSETS)
    early = CHECK_OFFSETS[-1] + EARLY_CHECK_EVERY
    while commence - early > now:
        offsets.append(early)
        early += EARLY_CHECK_EVERY
    due = [commence - offset for offset in offsets if commence - offset > now]
    return min(due, default=None)


def check_label(commence_time: str, checked_at: datetime) -> str:
    """'2h before tip' / '15 min before tip' for the check nearest now."""
    left = parse_timestamp(commence_time) - checked_at
    minutes = max(0, round(left.total_seconds() / 60))
    if minutes < 45:
        return f"{minutes} min before tip"
    return f"{round(minutes / 60)}h before tip"


def spread_point(pick_type: str, line: float | None) -> float | None:
    """A side's spread for move checks; a moneyline-only pick'em is 0."""
    return 0.0 if pick_type == "winner" else line


def side_quote(event: MarketEvent, team: str) -> tuple[str, float | None, int | None]:
    """How a side is offered: ``spread`` with a point (0 = pick'em), or
    ``winner`` with the moneyline price when there is no spread."""
    spread = event.markets.get(f"spread:{team}")
    if spread is not None and spread.point is not None:
        return "spread", spread.point, spread.price
    winner = event.markets.get(f"winner:{team}")
    return "winner", None, winner.price if winner is not None else None


def side_label(team: str, pick_type: str, line: float | None) -> str:
    """'Celtics -5.5', 'Lakers pk' (a 0 spread or no spread at all)."""
    short = team.split()[-1]
    if pick_type == "spread" and line:
        return f"{short} {line:+g}"
    return f"{short} pk"


def describe_bait(bait: dict[str, Any]) -> str:
    text = side_label(bait["selection"], bait["pick_type"], bait["line"])
    if bait["price"] is not None:
        text += f" ({bait['price']:+d})"
    return text


def lines_snapshot(event: MarketEvent) -> dict[str, Any]:
    """Both sides' spreads and moneylines and the total, as tapped."""
    snapshot: dict[str, Any] = {}
    for side, team in (("away", event.away_team), ("home", event.home_team)):
        spread = event.markets.get(f"spread:{team}")
        winner = event.markets.get(f"winner:{team}")
        snapshot[f"{side}_spread"] = spread.point if spread else None
        snapshot[f"{side}_spread_price"] = spread.price if spread else None
        snapshot[f"{side}_moneyline"] = winner.price if winner else None
    total = event.markets.get("total:Over")
    snapshot["total"] = total.point if total else None
    return snapshot


def encode_line(line: float | None) -> str:
    return "ml" if line is None else f"{line:g}"


def decode_line(value: str) -> float | None:
    return None if value == "ml" else float(value)


class BaitStore:
    """Bait rows in Mel's decisions database."""

    def __init__(self, store: DecisionStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS baits (
                    id TEXT PRIMARY KEY,
                    sport TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    commence_time TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    -- 'pass': no longer tempted by the last side at this line
                    action TEXT NOT NULL CHECK(action IN ('take','pass')),
                    selection TEXT NOT NULL,
                    pick_type TEXT NOT NULL CHECK(pick_type IN (
                        'spread','winner'
                    )),
                    line REAL,
                    price INTEGER,
                    lines_json TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    previous_id TEXT REFERENCES baits(id),
                    reason TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN (
                        'open','final'
                    )),
                    result TEXT CHECK(result IN (
                        'win','loss','push','ungradeable'
                    )),
                    away_score INTEGER,
                    home_score INTEGER,
                    closing_line REAL,
                    closing_price INTEGER,
                    closing_captured_at TEXT,
                    next_result_check_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS baits_game ON baits(event_id);
                CREATE INDEX IF NOT EXISTS baits_due
                    ON baits(status, next_result_check_at);

                -- One row per baited game: when to check its line next, and
                -- the number from the last alert (NULL: the last bait row's).
                CREATE TABLE IF NOT EXISTS bait_watch (
                    event_id TEXT PRIMARY KEY,
                    commence_time TEXT NOT NULL,
                    next_check_at TEXT,
                    alert_line REAL,
                    alerted_at TEXT
                );

                -- Mel's messages a reason can be replied to.
                CREATE TABLE IF NOT EXISTS bait_messages (
                    telegram_message_id INTEGER PRIMARY KEY,
                    bait_id TEXT NOT NULL REFERENCES baits(id)
                );
                """
            )
            # Games baited before scheduled checks existed.
            now = utc_now()
            for row in connection.execute(
                "SELECT event_id, MAX(commence_time) AS commence_time FROM baits "
                "WHERE event_id NOT IN (SELECT event_id FROM bait_watch) "
                "GROUP BY event_id"
            ).fetchall():
                connection.execute(
                    "INSERT INTO bait_watch (event_id, commence_time, "
                    "next_check_at) VALUES (?, ?, ?)",
                    (
                        row["event_id"],
                        row["commence_time"],
                        _stamp(next_check(row["commence_time"], now)),
                    ),
                )
            connection.commit()

    def record(
        self,
        event: MarketEvent,
        *,
        action: str,
        selection: str,
        pick_type: str,
        line: float | None,
        price: int | None,
    ) -> tuple[dict[str, Any], bool]:
        """Log a take or pass; returns (row, created). Tapping the same side
        at the same number again returns the existing row."""
        now = utc_now()
        commence = parse_timestamp(event.commence_time)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM baits WHERE event_id=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (event.id,),
            ).fetchone()
            if (
                previous is not None
                and previous["action"] == action
                and previous["selection"] == selection
                and previous["line"] == line
            ):
                connection.commit()
                return dict(previous), False
            bait_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO baits (
                    id, sport, event_id, commence_time, away_team, home_team,
                    action, selection, pick_type, line, price, lines_json,
                    ordinal, previous_id, created_at, next_result_check_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bait_id,
                    event.sport,
                    event.id,
                    event.commence_time,
                    event.away_team,
                    event.home_team,
                    action,
                    selection,
                    pick_type,
                    line,
                    price,
                    json.dumps(lines_snapshot(event)),
                    (previous["ordinal"] if previous else 0) + 1,
                    previous["id"] if previous else None,
                    timestamp(now),
                    timestamp(max(now, commence) + RESULT_DELAY),
                ),
            )
            row = connection.execute(
                "SELECT * FROM baits WHERE id=?", (bait_id,)
            ).fetchone()
            # A new bait row is the number you last saw: checks restart
            # from it.
            connection.execute(
                """
                INSERT INTO bait_watch (event_id, commence_time, next_check_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    commence_time=excluded.commence_time,
                    next_check_at=excluded.next_check_at,
                    alert_line=NULL
                """,
                (
                    event.id,
                    event.commence_time,
                    _stamp(next_check(event.commence_time, now)),
                ),
            )
            connection.commit()
        return dict(row), True

    def get(self, bait_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM baits WHERE id=?", (bait_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest(self, event_id: str, action: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM baits WHERE event_id=?"
        params: list[Any] = [event_id]
        if action:
            query += " AND action=?"
            params.append(action)
        with self.store.connect() as connection:
            row = connection.execute(
                query + " ORDER BY ordinal DESC LIMIT 1", params
            ).fetchone()
        return dict(row) if row else None

    def due_checks(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bait_watch WHERE next_check_at IS NOT NULL "
                "AND next_check_at<=? ORDER BY next_check_at",
                (timestamp(utc_now()),),
            ).fetchall()
        return [dict(row) for row in rows]

    def checked(self, event_id: str, alert_line: float | None = None) -> None:
        """Schedule the next check; an alert's number becomes the baseline
        so the same move is not alerted twice."""
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT commence_time FROM bait_watch WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                return
            now = utc_now()
            connection.execute(
                "UPDATE bait_watch SET next_check_at=? WHERE event_id=?",
                (_stamp(next_check(row["commence_time"], now)), event_id),
            )
            if alert_line is not None:
                connection.execute(
                    "UPDATE bait_watch SET alert_line=?, alerted_at=? "
                    "WHERE event_id=?",
                    (alert_line, timestamp(now), event_id),
                )
            connection.commit()

    def watch(self, event_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bait_watch WHERE event_id=?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def game_history(self, event_id: str) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM baits WHERE event_id=? ORDER BY ordinal",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def baited_events(self, event_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Each listed game's latest bait row."""
        return {
            event_id: row
            for event_id in event_ids
            if (row := self.latest(event_id)) is not None
        }

    def link_message(self, telegram_message_id: int, bait_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO bait_messages VALUES (?, ?)",
                (telegram_message_id, bait_id),
            )
            connection.execute(
                "UPDATE baits SET telegram_message_id=COALESCE("
                "telegram_message_id, ?) WHERE id=?",
                (telegram_message_id, bait_id),
            )
            connection.commit()

    def for_message(self, telegram_message_id: int) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT baits.* FROM bait_messages JOIN baits "
                "ON baits.id=bait_messages.bait_id "
                "WHERE bait_messages.telegram_message_id=?",
                (telegram_message_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_reason(self, bait_id: str, text: str) -> dict[str, Any]:
        """Append free-text reasons; several replies all count."""
        text = " ".join(text.split())
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE baits SET reason=CASE WHEN reason='' THEN ? "
                "ELSE reason || char(10) || ? END WHERE id=?",
                (text, text, bait_id),
            )
            connection.commit()
        bait = self.get(bait_id)
        if bait is None:
            raise LookupError("Bait not found")
        return bait

    def toggle_tag(self, bait_id: str, tag: str) -> dict[str, Any]:
        if tag not in TAG_LABELS:
            raise ValueError("Unknown bait tag")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tags_json FROM baits WHERE id=?", (bait_id,)
            ).fetchone()
            if row is None:
                connection.commit()
                raise LookupError("Bait not found")
            tags = json.loads(row["tags_json"])
            tags = [item for item in tags if item != tag] if tag in tags else [*tags, tag]
            connection.execute(
                "UPDATE baits SET tags_json=? WHERE id=?",
                (json.dumps(tags), bait_id),
            )
            connection.commit()
        return self.get(bait_id)  # type: ignore[return-value]

    def due_closes(self) -> list[dict[str, Any]]:
        cutoff = timestamp(utc_now() - CLOSE_DELAY)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM baits WHERE closing_captured_at IS NULL "
                "AND commence_time<=?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_close(self, bait_id: str, event: MarketEvent | None, side: str) -> None:
        """Bovada's last pre-tip number for the bait's side (``side`` is
        'away' or 'home'), so a later read can tell whether the market moved
        with the bait or against it."""
        line = price = None
        if event is not None:
            team = event.away_team if side == "away" else event.home_team
            bait = self.get(bait_id)
            if bait is not None and bait["pick_type"] == "spread":
                quote = event.markets.get(f"spread:{team}")
                if quote is not None:
                    line, price = quote.point, quote.price
            else:
                quote = event.markets.get(f"winner:{team}")
                if quote is not None:
                    price = quote.price
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE baits SET closing_line=?, closing_price=?, "
                "closing_captured_at=? WHERE id=?",
                (line, price, timestamp(utc_now()), bait_id),
            )
            connection.commit()

    def due_results(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM baits WHERE status='open' "
                "AND next_result_check_at<=? ORDER BY commence_time",
                (timestamp(utc_now()),),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_result(self, bait_id: str, minutes: int = 15) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE baits SET next_result_check_at=? WHERE id=?",
                (timestamp(utc_now() + timedelta(minutes=minutes)), bait_id),
            )
            connection.commit()

    def settle(self, bait_id: str, game: FinalGame) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM baits WHERE id=?", (bait_id,)
            ).fetchone()
            if row is None or row["status"] != "open":
                connection.commit()
                return
            result = grade_pick(
                pick_type=row["pick_type"],
                selection=row["selection"],
                line=row["line"],
                away_team=row["away_team"],
                home_team=row["home_team"],
                away_score=game.away_score,
                home_score=game.home_score,
            )
            connection.execute(
                "UPDATE baits SET status='final', result=?, away_score=?, "
                "home_score=?, settled_at=? WHERE id=?",
                (
                    result,
                    game.away_score,
                    game.home_score,
                    timestamp(utc_now()),
                    bait_id,
                ),
            )
            connection.commit()

    def all(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM baits ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]


def _stamp(value: datetime | None) -> str | None:
    return timestamp(value) if value is not None else None


def scheduled_move(
    event: MarketEvent, last: dict[str, Any], watch: dict[str, Any] | None
) -> float | None:
    """The current spread of the last bait's side when it moved at least a
    full point from the last number you saw (the last alert's, else the last
    bait row's); otherwise None."""
    pick_type, line, _ = side_quote(event, last["selection"])
    current = spread_point(pick_type, line)
    if pick_type == "winner" and event.markets.get(f"winner:{last['selection']}") is None:
        return None  # the book has no line for the game right now
    seen = (
        watch["alert_line"]
        if watch is not None and watch["alert_line"] is not None
        else spread_point(last["pick_type"], last["line"])
    )
    if current is None or seen is None or abs(current - seen) < ALERT_MOVE:
        return None
    return current


# --- Telegram presentation -------------------------------------------------


def board_keyboard(
    events: list[MarketEvent], baited: dict[str, dict[str, Any]], now
) -> InlineKeyboardMarkup:
    """Two side buttons per open game; a baited game gets its latest-line
    check instead, which offers both sides again at the current number."""
    rows = []
    for event in events:
        if parse_timestamp(event.commence_time) <= now:
            continue
        if event.id in baited:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🔄 Latest line: {event.away_team.split()[-1]} @ "
                        f"{event.home_team.split()[-1]}",
                        callback_data=f"baitline:{event.id}"[:64],
                    )
                ]
            )
            continue
        rows.append(side_buttons(event))
    return InlineKeyboardMarkup(rows)


def side_buttons(
    event: MarketEvent, action: str = "bait", label_prefix: str = ""
) -> list[InlineKeyboardButton]:
    """Away and home buttons carrying the number shown, so a tap records
    the line you saw. Event ids may be truncated to Telegram's 64 bytes."""
    buttons = []
    for code, team in (("a", event.away_team), ("h", event.home_team)):
        pick_type, line, _ = side_quote(event, team)
        buttons.append(
            InlineKeyboardButton(
                label_prefix + side_label(team, pick_type, line),
                callback_data=f"{action}:{code}:{encode_line(line)}:{event.id}"[:64],
            )
        )
    return buttons


def format_board(
    events: list[MarketEvent], baited: dict[str, dict[str, Any]], now, eastern
) -> str:
    lines = [
        "🎣 NBA lines (Bovada). Tap the side that looks too easy to pass up:"
    ]
    for event in events:
        start = parse_timestamp(event.commence_time)
        sides = " / ".join(
            side_label(team, *side_quote(event, team)[:2])
            for team in (event.away_team, event.home_team)
        )
        total = event.markets.get("total:Over")
        if total is not None and total.point is not None:
            sides += f", o/u {total.point:g}"
        line = (
            f"• {start.astimezone(eastern):%-I:%M %p} "
            f"{event.away_team} @ {event.home_team}: {sides}"
        )
        if start <= now:
            line += " · started"
        bait = baited.get(event.id)
        if bait is not None:
            line += (
                f" · 🎣 {describe_bait(bait)}"
                if bait["action"] == "take"
                else f" · 🙅 passed on {describe_bait(bait)}"
            )
        lines.append(line)
    return "\n".join(lines)


def confirmation_keyboard(bait: dict[str, Any]) -> InlineKeyboardMarkup:
    chosen = set(json.loads(bait["tags_json"]))
    tags = [
        InlineKeyboardButton(
            ("✅ " if key in chosen else "") + label,
            callback_data=f"baittag:{key}:{bait['id']}",
        )
        for key, label in BAIT_TAGS
    ]
    rows = [tags[index:index + 2] for index in range(0, len(tags), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Add a reason", callback_data=f"baitwhy:{bait['id']}"
            ),
            InlineKeyboardButton(
                "🔄 Latest line",
                callback_data=f"baitline:{bait['event_id']}"[:64],
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def format_confirmation(bait: dict[str, Any], history: list[dict[str, Any]], eastern) -> str:
    start = parse_timestamp(bait["commence_time"]).astimezone(eastern)
    matchup = f"{bait['away_team']} @ {bait['home_team']} · {start:%a %-I:%M %p} ET"
    if bait["action"] == "pass":
        head = f"🙅 Not tempted anymore: {describe_bait(bait)}"
    else:
        head = f"🎣 Taking the bait: {describe_bait(bait)}"
        takes = [
            row
            for row in history
            if row["action"] == "take" and row["ordinal"] <= bait["ordinal"]
        ]
        if len(takes) > 1:
            head += (
                f" (take #{len(takes)}, first was {describe_bait(takes[0])})"
            )
    lines = [head, matchup]
    tags = [TAG_LABELS[tag] for tag in json.loads(bait["tags_json"]) if tag in TAG_LABELS]
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    if bait["reason"]:
        lines.append("Reason: " + bait["reason"].replace("\n", " · "))
    lines.append(
        "Why? Tap tags, or reply to this message with a reason or voice "
        "note (optional)."
    )
    return "\n".join(lines)


def format_latest(
    event: MarketEvent, history: list[dict[str, Any]], eastern
) -> tuple[str, bool]:
    """The game's current line against the last bait; (text, moved)."""
    last = history[-1]
    pick_type, line, price = side_quote(event, last["selection"])
    now_label = side_label(last["selection"], pick_type, line)
    if price is not None:
        now_label += f" ({price:+d})"
    moved = (pick_type, line) != (last["pick_type"], last["line"]) or (
        pick_type == "winner"
        and None not in (price, last["price"])
        and price != last["price"]
    )
    when = parse_timestamp(last["created_at"]).astimezone(eastern)
    verb = "took" if last["action"] == "take" else "passed on"
    lines = [f"🔄 {event.away_team} @ {event.home_team}"]
    if moved:
        lines.append(
            f"📈 Line moved: you {verb} {describe_bait(last)} at "
            f"{when:%-I:%M %p} ET, now {now_label}."
        )
        lines.append("Still tempting? Tap a side to take the bait again, or pass.")
    else:
        lines.append(
            f"No change: still {now_label}, as when you {verb} it at "
            f"{when:%-I:%M %p} ET."
        )
    return "\n".join(lines), moved


def latest_keyboard(event: MarketEvent, last: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [side_buttons(event, "baitre", "🎣 ")]
    if last["action"] == "take":
        pick_type, line, _ = side_quote(event, last["selection"])
        code = "a" if last["selection"] == event.away_team else "h"
        rows.append(
            [
                InlineKeyboardButton(
                    "🙅 Not tempted anymore",
                    callback_data=f"baitpass:{code}:{encode_line(line)}:{event.id}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


# --- Stats -----------------------------------------------------------------


def _record(results: Counter) -> str:
    text = f"{results['win']}-{results['loss']}"
    if results["push"]:
        text += f"-{results['push']}"
    decided = results["win"] + results["loss"]
    if decided:
        text += f" ({results['win'] / decided:.0%})"
    return text


def _spread_bucket(bait: dict[str, Any]) -> str:
    line = bait["line"]
    if bait["pick_type"] != "spread" or not line:
        return "pick'em"
    size = abs(line)
    role = "favorite" if line < 0 else "underdog"
    if size <= 3:
        band = "1-3"
    elif size <= 6.5:
        band = "3.5-6.5"
    elif size <= 9.5:
        band = "7-9.5"
    else:
        band = "10+"
    return f"{role} {band}"


def _move(bait: dict[str, Any], baseline: dict[str, Any]) -> float | None:
    """Points the bait's side gained (+) or lost (-) since ``baseline``,
    both on the same team's spread."""
    if (
        bait["pick_type"] != "spread"
        or baseline["pick_type"] != "spread"
        or bait["line"] is None
        or baseline["line"] is None
    ):
        return None
    if baseline["selection"] == bait["selection"]:
        return bait["line"] - baseline["line"]
    return bait["line"] + baseline["line"]


def bait_stats(baits: list[dict[str, Any]]) -> dict[str, dict[str, Counter]]:
    """Graded takes grouped every way that might separate good bait from
    bad, plus how the passes would have done."""
    graded = [
        bait
        for bait in baits
        if bait["status"] == "final" and bait["result"] in {"win", "loss", "push"}
    ]
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bait in sorted(baits, key=lambda row: row["ordinal"]):
        by_game[bait["event_id"]].append(bait)
    groups: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for bait in graded:
        result = bait["result"]
        if bait["action"] == "pass":
            groups["Passes (the side you dropped)"]["would have gone"][result] += 1
            continue
        groups["Overall"]["all takes"][result] += 1
        history = by_game[bait["event_id"]]
        takes = [row for row in history if row["action"] == "take"]
        if takes and takes[-1]["id"] == bait["id"]:
            groups["Overall"]["final take per game"][result] += 1
        if bait["ordinal"] == 1:
            groups["Take"]["first look"][result] += 1
        else:
            move = _move(bait, takes[0])
            if bait["selection"] != takes[0]["selection"]:
                label = "re-take, switched sides"
            elif move is None or move == 0:
                label = "re-take, same number"
            elif move > 0:
                label = "re-take, number got better"
            else:
                label = "re-take, chased a worse number"
            groups["Take"][label][result] += 1
        groups["Favorite / underdog"][_spread_bucket(bait)][result] += 1
        side = "home" if bait["selection"] == bait["home_team"] else "away"
        groups["Home / away"][side][result] += 1
        if bait["closing_captured_at"] and bait["closing_line"] is not None:
            close = _move(
                {**bait, "line": bait["closing_line"]}, bait
            )
            if close is not None:
                label = (
                    "market moved toward your side"
                    if close < 0
                    else "market moved away from your side"
                    if close > 0
                    else "no move by tip"
                )
                groups["Line by tip"][label][result] += 1
        tags = json.loads(bait["tags_json"]) or ["untagged"]
        for tag in tags:
            groups["Tags"][TAG_LABELS.get(tag, tag)][result] += 1
        groups["Reason"]["wrote a reason" if bait["reason"] else "no reason"][result] += 1
    return groups


def format_bait_stats(baits: list[dict[str, Any]], eastern) -> str:
    takes = [bait for bait in baits if bait["action"] == "take"]
    if not takes:
        return "No bait logged yet. Use /bait to mark tempting NBA sides."
    pending = sum(bait["status"] == "open" for bait in takes)
    lines = [
        f"🎣 Bait report: {len(takes)} take(s), {pending} waiting on a result"
    ]
    for title, rows in bait_stats(baits).items():
        lines.append("")
        lines.append(title)
        for label, results in sorted(
            rows.items(), key=lambda item: -sum(item[1].values())
        ):
            lines.append(f"• {label}: {_record(results)}")
    recent = [bait for bait in takes if bait["status"] == "final"][-8:]
    if recent:
        lines.append("")
        lines.append("Recent graded bait")
        icons = {"win": "✅", "loss": "❌", "push": "➖", "ungradeable": "❔"}
        for bait in reversed(recent):
            day = parse_timestamp(bait["commence_time"]).astimezone(eastern)
            why = [
                TAG_LABELS[tag]
                for tag in json.loads(bait["tags_json"])
                if tag in TAG_LABELS
            ]
            if bait["reason"]:
                why.append(bait["reason"].replace("\n", " · "))
            text = (
                f"• {icons.get(bait['result'], '')} {day:%b %-d} "
                f"{describe_bait(bait)} ({bait['away_score']}-"
                f"{bait['home_score']})"
            )
            if bait["ordinal"] > 1:
                text += " [re-take]"
            if why:
                text += ": " + "; ".join(why)
            lines.append(text[:400])
    return "\n".join(lines)
