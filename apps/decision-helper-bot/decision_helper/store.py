from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .markets import next_market_check, quote_for_pick, spread_for_pick
from .models import (
    DecisionReview,
    Extraction,
    FinalGame,
    MarketEvent,
    closing_value,
    grade_pick,
    json_array,
    market_family,
    parse_timestamp,
    timestamp,
    utc_now,
)


class DecisionStore:
    def __init__(self, database_path: Path) -> None:
        if not database_path.is_absolute():
            raise ValueError("database_path must be absolute")
        self.database_path = database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _migrate_entries(self) -> None:
        """Rebuild a v1 entries table, whose status CHECK predates button choices."""
        if not self.database_path.exists():
            return
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone()
            if row is None or "awaiting_choice" in row["sql"]:
                return
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE entries_v2 (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    audio_kind TEXT NOT NULL,
                    transcript TEXT NOT NULL DEFAULT '',
                    immediate_feedback TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN (
                        'awaiting_choice','processing','saved','failed',
                        'trashed'
                    )),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    audio_suffix TEXT NOT NULL DEFAULT '',
                    UNIQUE(telegram_chat_id, telegram_message_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO entries_v2(
                    id, telegram_chat_id, telegram_message_id, audio_kind,
                    transcript, immediate_feedback, status, error,
                    created_at, updated_at
                )
                SELECT id, telegram_chat_id, telegram_message_id, audio_kind,
                    transcript, immediate_feedback, status, error,
                    created_at, updated_at
                FROM entries
                """
            )
            connection.execute("DROP TABLE entries")
            connection.execute("ALTER TABLE entries_v2 RENAME TO entries")
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.database_path.parent, 0o770)
        self._migrate_entries()
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    audio_kind TEXT NOT NULL,
                    transcript TEXT NOT NULL DEFAULT '',
                    immediate_feedback TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN (
                        'awaiting_choice','processing','saved','failed',
                        'trashed'
                    )),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    audio_suffix TEXT NOT NULL DEFAULT '',
                    UNIQUE(telegram_chat_id, telegram_message_id)
                );

                CREATE TABLE IF NOT EXISTS picks (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    telegram_chat_id INTEGER NOT NULL,
                    sport TEXT NOT NULL CHECK(sport IN ('nfl','nba')),
                    event_id TEXT NOT NULL,
                    commence_time TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    pick_type TEXT NOT NULL CHECK(pick_type IN (
                        'winner','spread','total'
                    )),
                    selection TEXT NOT NULL,
                    line REAL,
                    line_source TEXT NOT NULL CHECK(line_source IN (
                        'spoken','market','none'
                    )),
                    price_at_pick INTEGER,
                    confidence_probability REAL,
                    confidence_label TEXT NOT NULL DEFAULT '',
                    reasoning TEXT NOT NULL DEFAULT '',
                    uncertainties_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN (
                        'open','final','void'
                    )),
                    result TEXT CHECK(result IN (
                        'win','loss','push','ungradeable'
                    )),
                    away_score INTEGER,
                    home_score INTEGER,
                    closing_line REAL,
                    closing_price INTEGER,
                    closing_captured_at TEXT,
                    closing_value REAL,
                    next_market_check_at TEXT,
                    next_result_check_at TEXT NOT NULL,
                    analysis_status TEXT NOT NULL CHECK(analysis_status IN (
                        'not_ready','pending','complete'
                    )),
                    analysis_attempts INTEGER NOT NULL DEFAULT 0,
                    next_analysis_at TEXT,
                    process_score INTEGER,
                    luck_summary TEXT NOT NULL DEFAULT '',
                    what_worked TEXT NOT NULL DEFAULT '',
                    what_to_improve TEXT NOT NULL DEFAULT '',
                    bias_tags_json TEXT NOT NULL DEFAULT '[]',
                    feedback_sent_at TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    commitment TEXT NOT NULL DEFAULT 'pick' CHECK(
                        commitment IN ('pick','lean')
                    ),
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    logged_after_start INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL DEFAULT 'final' CHECK(
                        role IN ('final','superseded')
                    ),
                    spread_team TEXT,
                    spread_at_pick REAL,
                    my_line_low REAL,
                    my_line_high REAL
                );

                CREATE INDEX IF NOT EXISTS picks_status_result
                    ON picks(status, next_result_check_at);
                CREATE INDEX IF NOT EXISTS picks_market_due
                    ON picks(status, next_market_check_at);
                CREATE INDEX IF NOT EXISTS picks_analysis_due
                    ON picks(analysis_status, next_analysis_at);

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id TEXT PRIMARY KEY,
                    pick_id TEXT NOT NULL REFERENCES picks(id),
                    captured_at TEXT NOT NULL,
                    point REAL,
                    price INTEGER,
                    bookmaker_count INTEGER NOT NULL,
                    UNIQUE(pick_id, captured_at)
                );

                CREATE TABLE IF NOT EXISTS game_notes (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    sport TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    commence_time TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('context','hindsight')),
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS game_notes_game
                    ON game_notes(sport, away_team, home_team);

                CREATE TABLE IF NOT EXISTS daily_learnings (
                    day TEXT PRIMARY KEY,
                    learning_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS passes (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    sport TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    commence_time TEXT NOT NULL,
                    market TEXT NOT NULL CHECK(market IN ('side','total')),
                    reasoning TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS passes_game
                    ON passes(sport, away_team, home_team);
                """
            )
            entry_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(entries)")
            }
            if "pregame" not in entry_columns:
                # Speaker corrections that survive reparses: the note holds
                # views from before tip even if it was sent later, and words
                # the transcript missed.
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN pregame INTEGER NOT NULL "
                    "DEFAULT 0"
                )
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN clarification TEXT NOT NULL "
                    "DEFAULT ''"
                )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(picks)")
            }
            if "commitment" not in columns:
                connection.execute(
                    "ALTER TABLE picks ADD COLUMN commitment TEXT NOT NULL "
                    "DEFAULT 'pick' CHECK(commitment IN ('pick','lean'))"
                )
            if "ordinal" not in columns:
                connection.execute(
                    "ALTER TABLE picks ADD COLUMN ordinal INTEGER NOT NULL "
                    "DEFAULT 0"
                )
            if "logged_after_start" not in columns:
                connection.execute(
                    "ALTER TABLE picks ADD COLUMN logged_after_start INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            if "role" not in columns:
                connection.execute(
                    "ALTER TABLE picks ADD COLUMN role TEXT NOT NULL DEFAULT "
                    "'final' CHECK(role IN ('final','superseded'))"
                )
            if "spread_at_pick" not in columns:
                connection.execute("ALTER TABLE picks ADD COLUMN spread_team TEXT")
                connection.execute(
                    "ALTER TABLE picks ADD COLUMN spread_at_pick REAL"
                )
            if "my_line_low" not in columns:
                connection.execute("ALTER TABLE picks ADD COLUMN my_line_low REAL")
                connection.execute("ALTER TABLE picks ADD COLUMN my_line_high REAL")
            stale_before = timestamp(utc_now() - timedelta(hours=1))
            now = timestamp(utc_now())
            connection.execute(
                """
                UPDATE entries
                SET status='failed',
                    error='Processing was interrupted before completion',
                    updated_at=?
                WHERE status='processing' AND updated_at<?
                """,
                (now, stale_before),
            )
            connection.commit()
        os.chmod(self.database_path, 0o660)

    def create_entry(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        audio_kind: str,
        status: str = "processing",
        audio_suffix: str = "",
    ) -> tuple[dict[str, Any], bool]:
        now = timestamp(utc_now())
        entry_id = str(uuid.uuid4())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO entries(
                    id, telegram_chat_id, telegram_message_id, audio_kind,
                    status, audio_suffix, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    telegram_chat_id,
                    telegram_message_id,
                    audio_kind,
                    status,
                    audio_suffix,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                """
                SELECT * FROM entries
                WHERE telegram_chat_id=? AND telegram_message_id=?
                """,
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
            connection.commit()
        return dict(row), created

    def choose(self, entry_id: str, choice: str) -> dict[str, Any] | None:
        """Atomically resolve an awaiting voice note; None if already handled."""
        status = {"parse": "processing", "trash": "trashed"}[choice]
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE entries SET status=?, updated_at=?
                WHERE id=? AND status='awaiting_choice'
                """,
                (status, timestamp(utc_now()), entry_id),
            )
            row = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            connection.commit()
        return dict(row) if cursor.rowcount == 1 else None

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Entry not found")
        return dict(row)

    def set_transcript(self, entry_id: str, transcript: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE entries SET transcript=?, updated_at=? WHERE id=?",
                (transcript, timestamp(utc_now()), entry_id),
            )
            connection.commit()

    def fail_entry(self, entry_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE entries
                SET status='failed', error=?, updated_at=?
                WHERE id=?
                """,
                (error, timestamp(utc_now()), entry_id),
            )
            connection.commit()

    def save_extraction(
        self,
        *,
        entry_id: str,
        transcript: str,
        extraction: Extraction,
        events: list[MarketEvent],
        logged_at: datetime | None = None,
        refresh_market: bool = False,
    ) -> list[dict[str, Any]]:
        """Save picks as of ``logged_at`` (when the note was sent).

        ``refresh_market`` means ``events`` hold historical lines, so the
        current BetOnline line is re-read on the next sync.
        """
        by_id = {event.id: event for event in events}
        now_dt = utc_now()
        now = timestamp(now_dt)
        logged_dt = min(logged_at or now_dt, now_dt)
        logged = timestamp(logged_dt)
        saved_ids: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if entry is None:
                raise LookupError("Entry not found")
            if entry["status"] == "saved":
                rows = connection.execute(
                    "SELECT * FROM picks WHERE entry_id=? AND status!='void' "
                    "ORDER BY created_at, ordinal, id",
                    (entry_id,),
                ).fetchall()
                connection.commit()
                return [dict(row) for row in rows]
            for ordinal, pick in enumerate(extraction.picks):
                event = by_id[pick.event_id]
                if _settled(connection, event, pick.pick_type, as_of=logged):
                    # Already graded: keep the thought, but don't count it.
                    _insert_note(
                        connection,
                        entry_id,
                        event,
                        "hindsight",
                        _hindsight_summary(pick),
                        logged,
                    )
                    continue
                quote = quote_for_pick(event, pick.pick_type, pick.selection)
                line = pick.line
                line_source = "spoken" if line is not None else "none"
                if line is None and quote is not None and quote.point is not None:
                    line = quote.point
                    line_source = "market"
                price = quote.price if quote is not None else None
                spread_team, spread_point = spread_for_pick(
                    event, pick.pick_type, pick.selection
                ) or (None, None)
                commence = parse_timestamp(event.commence_time)
                result_delay = (
                    timedelta(hours=3, minutes=30)
                    if pick.sport == "nfl"
                    else timedelta(hours=2, minutes=45)
                )
                pick_id = str(uuid.uuid4())
                saved_ids.append(pick_id)
                connection.execute(
                    """
                    INSERT INTO picks(
                        id, entry_id, telegram_chat_id, sport, event_id,
                        commence_time, away_team, home_team, pick_type,
                        selection, line, line_source, price_at_pick,
                        confidence_probability, confidence_label, reasoning,
                        uncertainties_json, status, closing_line, closing_price,
                        closing_captured_at, next_market_check_at,
                        next_result_check_at, analysis_status, created_at,
                        commitment, ordinal, logged_after_start,
                        spread_team, spread_at_pick, my_line_low, my_line_high
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'open', ?, ?, ?, ?, ?, 'not_ready', ?, ?, ?, ?, ?, ?,
                        ?, ?
                    )
                    """,
                    (
                        pick_id,
                        entry_id,
                        int(entry["telegram_chat_id"]),
                        pick.sport,
                        event.id,
                        event.commence_time,
                        event.away_team,
                        event.home_team,
                        pick.pick_type,
                        pick.selection,
                        line,
                        line_source,
                        price,
                        pick.confidence_probability,
                        pick.confidence_label,
                        pick.reasoning,
                        json_array(pick.uncertainties),
                        quote.point if quote else None,
                        quote.price if quote else None,
                        logged if quote else None,
                        now
                        if refresh_market
                        else next_market_check(event.commence_time, now=now_dt),
                        timestamp(commence + result_delay),
                        logged,
                        pick.commitment,
                        ordinal,
                        int(commence <= logged_dt and not entry["pregame"]),
                        spread_team,
                        spread_point,
                        pick.my_line_low,
                        pick.my_line_high,
                    ),
                )
                if quote is not None:
                    connection.execute(
                        """
                        INSERT INTO market_snapshots(
                            id, pick_id, captured_at, point, price,
                            bookmaker_count
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            pick_id,
                            logged,
                            quote.point,
                            quote.price,
                            quote.bookmaker_count,
                        ),
                    )
            for note in extraction.game_notes:
                event = by_id[note.event_id]
                _insert_note(
                    connection,
                    entry_id,
                    event,
                    "hindsight"
                    if _settled(connection, event, as_of=logged)
                    else "context",
                    note.summary,
                    logged,
                )
            passed: list[tuple[MarketEvent, str]] = []
            for item in extraction.passes:
                event = by_id[item.event_id]
                if _settled(
                    connection,
                    event,
                    "total" if item.market == "total" else "spread",
                    as_of=logged,
                ):
                    _insert_note(
                        connection,
                        entry_id,
                        event,
                        "hindsight",
                        f"Passed on the {item.market}"
                        + (f": {item.reasoning}" if item.reasoning else ""),
                        logged,
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO passes(
                        id, entry_id, sport, event_id, away_team, home_team,
                        commence_time, market, reasoning, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        entry_id,
                        event.sport,
                        event.id,
                        event.away_team,
                        event.home_team,
                        event.commence_time,
                        item.market,
                        item.reasoning,
                        logged,
                    ),
                )
                passed.append((event, item.market))
            for pick_id in saved_ids:
                # A reparsed note can land before graded views; ordering by
                # note time decides which view is final.
                _refresh_roles(connection, pick_id, force=True)
            for event, market in passed:
                _refresh_game(
                    connection,
                    event.sport,
                    event.away_team,
                    event.home_team,
                    event.commence_time,
                    market,
                )
            connection.execute(
                """
                UPDATE entries
                SET transcript=?, immediate_feedback=?, status='saved',
                    error='', updated_at=?
                WHERE id=?
                """,
                (
                    transcript,
                    extraction.immediate_feedback,
                    now,
                    entry_id,
                ),
            )
            rows = connection.execute(
                "SELECT * FROM picks WHERE entry_id=? AND status!='void' "
                    "ORDER BY created_at, ordinal, id",
                (entry_id,),
            ).fetchall()
            connection.commit()
        return [dict(row) for row in rows]

    def entry_notes(self, entry_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM game_notes WHERE entry_id=? "
                "ORDER BY created_at, rowid",
                (entry_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def game_record(
        self,
        sport: str,
        away_team: str,
        home_team: str,
        commence_time: str,
        *,
        detailed: bool = False,
    ) -> dict[str, Any]:
        """Everything the speaker said about one game, oldest first.
        ``detailed`` adds lines, closing value, and post-game reviews."""
        with self.connect() as connection:
            picks = _same_game(
                connection.execute(
                    """
                    SELECT picks.*, entries.created_at AS note_at
                    FROM picks JOIN entries ON entries.id=picks.entry_id
                    WHERE picks.status!='void' AND picks.sport=?
                      AND picks.away_team=? AND picks.home_team=?
                    ORDER BY picks.created_at, picks.ordinal, picks.id
                    """,
                    (sport, away_team, home_team),
                ).fetchall(),
                commence_time,
            )
            notes = _same_game(
                connection.execute(
                    """
                    SELECT * FROM game_notes
                    WHERE sport=? AND away_team=? AND home_team=?
                    ORDER BY created_at, rowid
                    """,
                    (sport, away_team, home_team),
                ).fetchall(),
                commence_time,
            )
            passes = _same_game(
                connection.execute(
                    """
                    SELECT * FROM passes
                    WHERE sport=? AND away_team=? AND home_team=?
                    ORDER BY created_at, rowid
                    """,
                    (sport, away_team, home_team),
                ).fetchall(),
                commence_time,
            )
            entry_ids = list(
                dict.fromkeys(
                    [row["entry_id"] for row in picks]
                    + [row["entry_id"] for row in notes]
                    + [row["entry_id"] for row in passes]
                )
            )
            entries = (
                connection.execute(
                    f"""
                    SELECT created_at, transcript FROM entries
                    WHERE id IN ({",".join("?" * len(entry_ids))})
                      AND transcript!=''
                    ORDER BY created_at, rowid
                    """,
                    entry_ids,
                ).fetchall()
                if entry_ids
                else []
            )
        return {
            "decisions": [_decision_dict(row, detailed) for row in picks],
            "game_notes": [
                {
                    "logged_at": row["created_at"],
                    "kind": row["kind"],
                    "summary": row["summary"],
                }
                for row in notes
            ],
            "passes": [
                {
                    "logged_at": row["created_at"],
                    "market": row["market"],
                    "reasoning": row["reasoning"],
                }
                for row in passes
            ],
            "notes": [
                {"logged_at": row["created_at"], "transcript": row["transcript"]}
                for row in entries
            ],
        }

    def prior_views(
        self, events: list[MarketEvent], before: datetime | None = None
    ) -> dict[str, dict[str, Any]]:
        """Earlier recorded sides and notes for each candidate game (only
        those sent before ``before``, so a reparsed note sees what it saw)."""
        cutoff = before or utc_now()

        def earlier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                row for row in rows if parse_timestamp(row["logged_at"]) < cutoff
            ]

        views: dict[str, dict[str, Any]] = {}
        for event in events:
            record = self.game_record(
                event.sport, event.away_team, event.home_team, event.commence_time
            )
            decisions = earlier(record["decisions"])
            notes = earlier(record["game_notes"])
            passes = earlier(record["passes"])
            if decisions or notes or passes:
                views[event.id] = {
                    "decisions": [
                        {
                            key: decision[key]
                            for key in (
                                "logged_at",
                                "side",
                                "commitment",
                                "role",
                                "reasoning",
                                "result",
                            )
                        }
                        for decision in decisions
                    ],
                    "game_notes": notes,
                    **({"passes": passes} if passes else {}),
                }
        return views

    def entry_passes(self, entry_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM passes WHERE entry_id=? ORDER BY created_at, rowid",
                (entry_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def entry_picks(self, entry_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM picks WHERE entry_id=? AND status!='void' "
                    "ORDER BY created_at, ordinal, id",
                (entry_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_market_picks(
        self, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        current = timestamp(now or utc_now())
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                WHERE status='open'
                  AND next_market_check_at IS NOT NULL
                  AND next_market_check_at<=?
                ORDER BY commence_time
                """,
                (current,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_market_snapshot(
        self,
        pick_id: str,
        event: MarketEvent | None,
        *,
        captured_at: datetime | None = None,
    ) -> None:
        now_dt = captured_at or utc_now()
        now = timestamp(now_dt)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM picks WHERE id=?", (pick_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Pick not found")
            quote = (
                quote_for_pick(event, row["pick_type"], row["selection"])
                if event is not None
                else None
            )
            if quote is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO market_snapshots(
                        id, pick_id, captured_at, point, price,
                        bookmaker_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        pick_id,
                        now,
                        quote.point,
                        quote.price,
                        quote.bookmaker_count,
                    ),
                )
                connection.execute(
                    """
                    UPDATE picks
                    SET closing_line=?, closing_price=?,
                        closing_captured_at=?, next_market_check_at=?
                    WHERE id=?
                    """,
                    (
                        quote.point,
                        quote.price,
                        now,
                        next_market_check(row["commence_time"], now=now_dt),
                        pick_id,
                    ),
                )
                # Noted before BetOnline posted: take the first line posted.
                if (
                    row["line"] is None
                    and row["pick_type"] != "winner"
                    and quote.point is not None
                ):
                    connection.execute(
                        "UPDATE picks SET line=?, line_source='market' WHERE id=?",
                        (quote.point, pick_id),
                    )
                if row["price_at_pick"] is None and quote.price is not None:
                    connection.execute(
                        "UPDATE picks SET price_at_pick=? WHERE id=?",
                        (quote.price, pick_id),
                    )
            if event is not None and row["spread_at_pick"] is None:
                spread = spread_for_pick(event, row["pick_type"], row["selection"])
                if spread is not None:
                    connection.execute(
                        "UPDATE picks SET spread_team=?, spread_at_pick=? WHERE id=?",
                        (spread[0], spread[1], pick_id),
                    )
            if quote is None:
                connection.execute(
                    "UPDATE picks SET next_market_check_at=? WHERE id=?",
                    (
                        next_market_check(row["commence_time"], now=now_dt),
                        pick_id,
                    ),
                )
            connection.commit()

    def due_result_picks(
        self, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        current = timestamp(now or utc_now())
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                WHERE status='open' AND next_result_check_at<=?
                ORDER BY commence_time
                """,
                (current,),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_result(self, pick_id: str, minutes: int = 15) -> None:
        next_check = timestamp(utc_now() + timedelta(minutes=minutes))
        with self.connect() as connection:
            connection.execute(
                "UPDATE picks SET next_result_check_at=? WHERE id=?",
                (next_check, pick_id),
            )
            connection.commit()

    def settle_pick(self, pick_id: str, game: FinalGame) -> dict[str, Any]:
        now = timestamp(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM picks WHERE id=?", (pick_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Pick not found")
            if row["status"] != "open":
                connection.commit()
                return dict(row)
            result = grade_pick(
                pick_type=row["pick_type"],
                selection=row["selection"],
                line=row["line"],
                away_team=row["away_team"],
                home_team=row["home_team"],
                away_score=game.away_score,
                home_score=game.home_score,
            )
            clv = (
                None
                # Logged at or after tip: the line is the close, so CLV is 0
                # by construction and says nothing.
                if parse_timestamp(row["created_at"])
                >= parse_timestamp(row["commence_time"])
                else closing_value(
                    pick_type=row["pick_type"],
                    selection=row["selection"],
                    pick_line=row["line"],
                    closing_line=row["closing_line"],
                    pick_price=row["price_at_pick"],
                    closing_price=row["closing_price"],
                )
            )
            # Superseded sides are graded for flip analysis but not reviewed;
            # the final position's review covers the whole path.
            connection.execute(
                """
                UPDATE picks
                SET status='final', result=?, away_score=?, home_score=?,
                    closing_value=?, analysis_status=?,
                    next_analysis_at=?, settled_at=?,
                    next_market_check_at=NULL
                WHERE id=?
                """,
                (
                    result,
                    game.away_score,
                    game.home_score,
                    clv,
                    "pending" if row["role"] == "final" else "complete",
                    now if row["role"] == "final" else None,
                    now,
                    pick_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM picks WHERE id=?", (pick_id,)
            ).fetchone()
            connection.commit()
        return dict(updated)

    def due_analysis(
        self, now: datetime | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        current = timestamp(now or utc_now())
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT picks.*, entries.transcript
                FROM picks JOIN entries ON entries.id=picks.entry_id
                WHERE picks.analysis_status='pending'
                  AND picks.role='final'
                  AND picks.next_analysis_at<=?
                ORDER BY picks.settled_at, picks.id
                LIMIT ?
                """,
                (current, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_analysis(self, pick_id: str, minutes: int = 30) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE picks
                SET analysis_attempts=analysis_attempts+1,
                    next_analysis_at=?
                WHERE id=?
                """,
                (
                    timestamp(utc_now() + timedelta(minutes=minutes)),
                    pick_id,
                ),
            )
            connection.commit()

    def complete_analysis(
        self, pick_id: str, review: DecisionReview
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE picks
                SET analysis_status='complete', process_score=?,
                    luck_summary=?, what_worked=?, what_to_improve=?,
                    bias_tags_json=?, next_analysis_at=NULL
                WHERE id=?
                """,
                (
                    review.process_score,
                    review.luck_summary,
                    review.what_worked,
                    review.what_to_improve,
                    json_array(review.bias_tags),
                    pick_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM picks WHERE id=?", (pick_id,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise LookupError("Pick not found")
        return dict(row)

    def discussed_games(self) -> list[dict[str, Any]]:
        """Every live pick and game note with its game and grading state."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT sport, away_team, home_team, commence_time, status,
                       analysis_status, analysis_attempts
                FROM picks WHERE status!='void'
                UNION ALL
                SELECT sport, away_team, home_team, commence_time,
                       'final', 'complete', 0
                FROM game_notes
                UNION ALL
                SELECT sport, away_team, home_team, commence_time,
                       'final', 'complete', 0
                FROM passes
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_learning(self, day: str, learning: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_learnings(day, learning_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    learning_json=excluded.learning_json,
                    created_at=excluded.created_at
                """,
                (day, json.dumps(learning, ensure_ascii=False),
                 timestamp(utc_now())),
            )
            connection.commit()

    def learning_days(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT day FROM daily_learnings").fetchall()
        return {row["day"] for row in rows}

    def recent_learnings(
        self, limit: int = 7, before_day: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily learnings, newest first (only days before ``before_day``)."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM daily_learnings
                WHERE day<? ORDER BY day DESC LIMIT ?
                """,
                (before_day or "9999-99-99", limit),
            ).fetchall()
        return [
            {"day": row["day"], **json.loads(row["learning_json"])}
            for row in rows
        ]

    def learning(self, day: str | None = None) -> dict[str, Any] | None:
        """One day's learnings, or the latest when ``day`` is None."""
        if day is None:
            latest = self.recent_learnings(1)
            return latest[0] if latest else None
        found = self.recent_learnings(1, before_day=day + "~")
        return found[0] if found and found[0]["day"] == day else None

    def recent_lessons(self, limit: int = 5) -> list[dict[str, Any]]:
        """Most recent completed reviews of final decisions."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                WHERE role='final' AND analysis_status='complete'
                  AND process_score IS NOT NULL
                ORDER BY settled_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "game": f"{row['away_team']} @ {row['home_team']}",
                "side": _describe_side(row),
                "result": row["result"],
                "process_score": row["process_score"],
                "what_worked": row["what_worked"],
                "what_to_improve": row["what_to_improve"],
                "bias_tags": json.loads(row["bias_tags_json"]),
            }
            for row in rows
        ]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be 1-50")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                ORDER BY created_at DESC, ordinal DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_pick(self, prefix: str) -> dict[str, Any]:
        normalized = prefix.strip().lower()
        if len(normalized) < 4:
            raise ValueError("Pick prefix must contain at least 4 characters")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                WHERE lower(id) LIKE ?
                ORDER BY created_at DESC LIMIT 2
                """,
                (normalized + "%",),
            ).fetchall()
        if not rows:
            raise LookupError("Pick not found")
        if len(rows) > 1:
            raise LookupError("Pick prefix is ambiguous")
        return dict(rows[0])

    def void(self, prefix: str) -> dict[str, Any]:
        pick = self.resolve_pick(prefix)
        if pick["status"] == "final":
            raise ValueError("A settled pick cannot be voided")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE picks
                SET status='void', next_market_check_at=NULL
                WHERE id=?
                """,
                (pick["id"],),
            )
            _refresh_roles(connection, pick["id"])
            connection.commit()
        return self.resolve_pick(pick["id"])

    def latest_entry(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM entries
                WHERE status!='processing' AND transcript!=''
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def entry_for_message(
        self, chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        """The note recorded from this Telegram message (a voice note or text)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entries WHERE telegram_chat_id=? "
                "AND telegram_message_id=?",
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def reopen_entry(self, entry_id: str) -> dict[str, Any]:
        """Void an entry's picks, graded ones included, so its saved
        transcript can be reparsed; the new picks are graded again."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if entry is None:
                raise LookupError("Entry not found")
            if entry["status"] == "processing":
                raise ValueError("That note is still being processed")
            if not entry["transcript"]:
                raise ValueError("That note has no saved transcript")
            voided = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM picks WHERE entry_id=? AND status!='void'",
                    (entry_id,),
                )
            ]
            connection.execute(
                """
                UPDATE picks
                SET status='void', next_market_check_at=NULL
                WHERE entry_id=? AND status!='void'
                """,
                (entry_id,),
            )
            for pick_id in voided:
                _refresh_roles(connection, pick_id, force=True)
            _drop_passes(connection, entry_id)
            connection.execute(
                "DELETE FROM game_notes WHERE entry_id=?", (entry_id,)
            )
            connection.execute(
                """
                UPDATE entries
                SET status='processing', error='', updated_at=?
                WHERE id=?
                """,
                (timestamp(utc_now()), entry_id),
            )
            row = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            connection.commit()
        return dict(row)

    def remove_entry(self, entry_id: str) -> list[dict[str, Any]]:
        """Delete a parsed note: its picks (even graded ones), attached game
        notes, and transcript, as if it was never sent. Returns the picks."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = connection.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if entry is None or entry["status"] != "saved":
                raise ValueError("That note was already removed")
            picks = connection.execute(
                "SELECT * FROM picks WHERE entry_id=? AND status!='void'",
                (entry_id,),
            ).fetchall()
            siblings = set()
            for pick in picks:
                for row in _group_rows(connection, pick["id"]):
                    if row["entry_id"] != entry_id:
                        siblings.add(row["id"])
                        break
            connection.execute(
                "DELETE FROM market_snapshots WHERE pick_id IN "
                "(SELECT id FROM picks WHERE entry_id=?)",
                (entry_id,),
            )
            connection.execute("DELETE FROM picks WHERE entry_id=?", (entry_id,))
            connection.execute(
                "DELETE FROM game_notes WHERE entry_id=?", (entry_id,)
            )
            connection.execute(
                """
                UPDATE entries
                SET status='trashed', transcript='', immediate_feedback='',
                    updated_at=?
                WHERE id=?
                """,
                (timestamp(utc_now()), entry_id),
            )
            for pick_id in siblings:
                _refresh_roles(connection, pick_id, force=True)
            _drop_passes(connection, entry_id)
            connection.commit()
        return [dict(row) for row in picks]

    def decision_path(self, pick_id: str) -> list[dict[str, Any]]:
        """Earlier sides abandoned on the way to this final position."""
        with self.connect() as connection:
            return [
                dict(row)
                for row in _group_rows(connection, pick_id)
                if row["role"] == "superseded"
            ]

    def flip_stats(self, recent: int = 8) -> dict[str, Any]:
        """How changing sides has gone: graded first view vs final position."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM picks
                WHERE status='final' AND result IN ('win','loss','push')
                ORDER BY created_at, ordinal, id
                """
            ).fetchall()
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(_group_key(row), []).append(dict(row))
        flips: list[dict[str, Any]] = []
        first_record = Counter()
        final_record = Counter()
        for group in groups.values():
            finals = [pick for pick in group if pick["role"] == "final"]
            if not finals:
                continue
            first, final = group[0], finals[-1]
            first_record[first["result"]] += 1
            final_record[final["result"]] += 1
            if first["selection"] == final["selection"]:
                continue
            outcome = (
                "helped"
                if final["result"] == "win" and first["result"] == "loss"
                else "hurt"
                if final["result"] == "loss" and first["result"] == "win"
                else "no difference"
            )
            flips.append(
                {
                    "game": f"{final['away_team']} @ {final['home_team']}",
                    "commence_time": final["commence_time"],
                    "from": _describe_side(first),
                    "to": _describe_side(final),
                    "reason_for_switch": final["reasoning"],
                    "first_result": first["result"],
                    "final_result": final["result"],
                    "outcome": outcome,
                }
            )
        return {
            "flips": len(flips),
            "helped": sum(flip["outcome"] == "helped" for flip in flips),
            "hurt": sum(flip["outcome"] == "hurt" for flip in flips),
            "no_difference": sum(
                flip["outcome"] == "no difference" for flip in flips
            ),
            "first_view_record": _record(first_record),
            "final_position_record": _record(final_record),
            "recent_flips": flips[-recent:],
        }

    def stats(self, commitment: str | None = "pick") -> dict[str, Any]:
        """Final positions' record; commitment None covers bets and leans."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM picks WHERE status!='void' AND role='final' "
                "AND (? IS NULL OR commitment=?)",
                (commitment, commitment),
            ).fetchall()
        picks = [dict(row) for row in rows]
        finals = [
            pick
            for pick in picks
            if pick["status"] == "final"
            and pick["result"] in {"win", "loss", "push"}
        ]
        decisions = [
            pick for pick in finals if pick["result"] in {"win", "loss"}
        ]
        brier_values = [
            (
                float(pick["confidence_probability"])
                - (1.0 if pick["result"] == "win" else 0.0)
            )
            ** 2
            for pick in decisions
            if pick["confidence_probability"] is not None
        ]
        process_scores = [
            int(pick["process_score"])
            for pick in finals
            if pick["process_score"] is not None
        ]
        clv_values = [
            float(pick["closing_value"])
            for pick in finals
            if pick["closing_value"] is not None
        ]
        biases: Counter[str] = Counter()
        for pick in finals:
            for tag in json.loads(pick["bias_tags_json"]):
                biases[str(tag)] += 1
        return {
            "total": len(picks),
            "open": sum(pick["status"] == "open" for pick in picks),
            "wins": sum(pick["result"] == "win" for pick in finals),
            "losses": sum(pick["result"] == "loss" for pick in finals),
            "pushes": sum(pick["result"] == "push" for pick in finals),
            "brier": (
                sum(brier_values) / len(brier_values)
                if brier_values
                else None
            ),
            "calibrated_count": len(brier_values),
            "process_average": (
                sum(process_scores) / len(process_scores)
                if process_scores
                else None
            ),
            "clv_average": (
                sum(clv_values) / len(clv_values) if clv_values else None
            ),
            "positive_clv": sum(value > 0 for value in clv_values),
            "clv_count": len(clv_values),
            "biases": biases.most_common(5),
        }


def _group_key(row: Any) -> tuple[str, str, str, str, str]:
    return (
        row["sport"],
        row["away_team"],
        row["home_team"],
        row["commence_time"],
        market_family(row["pick_type"]),
    )


def _group_rows(connection: sqlite3.Connection, pick_id: str) -> list[Any]:
    """Non-void rows for the same game and market family, oldest first."""
    anchor = connection.execute(
        "SELECT * FROM picks WHERE id=?", (pick_id,)
    ).fetchone()
    if anchor is None:
        return []
    types = ("total",) if anchor["pick_type"] == "total" else ("winner", "spread")
    return connection.execute(
        f"""
        SELECT * FROM picks
        WHERE status!='void' AND sport=? AND away_team=? AND home_team=?
          AND commence_time=?
          AND pick_type IN ({",".join("?" * len(types))})
        ORDER BY created_at, ordinal, id
        """,
        (
            anchor["sport"],
            anchor["away_team"],
            anchor["home_team"],
            anchor["commence_time"],
            *types,
        ),
    ).fetchall()


def _refresh_roles(
    connection: sqlite3.Connection, pick_id: str, force: bool = False
) -> None:
    """The newest view of a game's side or total is final; older ones are
    superseded. Settled decisions are left alone unless ``force``d (a removed
    note); a graded row that becomes final is queued for review."""
    rows = _group_rows(connection, pick_id)
    if not rows or (
        not force and any(row["status"] == "final" for row in rows)
    ):
        return
    passed_at = _latest_pass(
        connection,
        rows[-1]["sport"],
        rows[-1]["away_team"],
        rows[-1]["home_team"],
        rows[-1]["commence_time"],
        market_family(rows[-1]["pick_type"]),
    )
    for row in rows:
        # A pass newer than every view leaves the game with no position.
        role = (
            "final"
            if row is rows[-1]
            and (passed_at is None or passed_at < row["created_at"])
            else "superseded"
        )
        connection.execute("UPDATE picks SET role=? WHERE id=?", (role, row["id"]))
        if (
            role == "final"
            and row["status"] == "final"
            and row["process_score"] is None
        ):
            connection.execute(
                "UPDATE picks SET analysis_status='pending', "
                "next_analysis_at=? WHERE id=?",
                (timestamp(utc_now()), row["id"]),
            )
        elif role == "superseded" and row["analysis_status"] == "pending":
            # Only final positions are reviewed.
            connection.execute(
                "UPDATE picks SET analysis_status='complete', "
                "next_analysis_at=NULL WHERE id=?",
                (row["id"],),
            )


def _latest_pass(
    connection: sqlite3.Connection,
    sport: str,
    away_team: str,
    home_team: str,
    commence_time: str,
    market: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT MAX(created_at) FROM passes
        WHERE sport=? AND away_team=? AND home_team=? AND commence_time=?
          AND market=?
        """,
        (sport, away_team, home_team, commence_time, market),
    ).fetchone()
    return row[0]


def _drop_passes(connection: sqlite3.Connection, entry_id: str) -> None:
    rows = connection.execute(
        "SELECT sport, away_team, home_team, commence_time, market "
        "FROM passes WHERE entry_id=?",
        (entry_id,),
    ).fetchall()
    connection.execute("DELETE FROM passes WHERE entry_id=?", (entry_id,))
    for row in rows:
        _refresh_game(connection, *tuple(row))


def _refresh_game(
    connection: sqlite3.Connection,
    sport: str,
    away_team: str,
    home_team: str,
    commence_time: str,
    market: str,
) -> None:
    """Re-rank one game's side or total after a pass was added or removed."""
    types = ("total",) if market == "total" else ("winner", "spread")
    row = connection.execute(
        f"""
        SELECT id FROM picks
        WHERE status!='void' AND sport=? AND away_team=? AND home_team=?
          AND commence_time=? AND pick_type IN ({",".join("?" * len(types))})
        LIMIT 1
        """,
        (sport, away_team, home_team, commence_time, *types),
    ).fetchone()
    if row is not None:
        _refresh_roles(connection, row[0], force=True)


def _same_game(rows: list[Any], commence_time: str) -> list[Any]:
    """Rows whose start is within 12 hours (ESPN start times can shift)."""
    anchor = parse_timestamp(commence_time)
    return [
        row
        for row in rows
        if abs(parse_timestamp(row["commence_time"]) - anchor)
        < timedelta(hours=12)
    ]


def _settled(
    connection: sqlite3.Connection,
    event: MarketEvent,
    pick_type: str | None = None,
    *,
    as_of: str,
) -> bool:
    """Whether this game (or market family) had a graded decision by
    ``as_of`` (when the note was sent), so a reparsed note keeps its role."""
    types = (
        ("winner", "spread", "total")
        if pick_type is None
        else ("total",)
        if pick_type == "total"
        else ("winner", "spread")
    )
    rows = connection.execute(
        f"""
        SELECT commence_time FROM picks
        WHERE status='final' AND sport=? AND away_team=? AND home_team=?
          AND pick_type IN ({",".join("?" * len(types))})
          AND (settled_at IS NULL OR settled_at<=?)
        """,
        (event.sport, event.away_team, event.home_team, *types, as_of),
    ).fetchall()
    return bool(_same_game(rows, event.commence_time))


def _insert_note(
    connection: sqlite3.Connection,
    entry_id: str,
    event: MarketEvent,
    kind: str,
    summary: str,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO game_notes(
            id, entry_id, sport, event_id, away_team, home_team,
            commence_time, kind, summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            entry_id,
            event.sport,
            event.id,
            event.away_team,
            event.home_team,
            event.commence_time,
            kind,
            summary,
            created_at,
        ),
    )


def _hindsight_summary(pick: Any) -> str:
    side = _describe_side(
        {"pick_type": pick.pick_type, "selection": pick.selection, "line": pick.line}
    )
    return f"After the result: {side}. {pick.reasoning}".strip()


def _spread_dict(pick: Any) -> dict[str, Any] | None:
    if pick["spread_at_pick"] is None:
        return None
    return {"team": pick["spread_team"], "point": pick["spread_at_pick"]}


def _decision_dict(row: Any, detailed: bool) -> dict[str, Any]:
    decision = {
        "logged_at": row["created_at"],
        "side": _describe_side(row),
        "pick_type": row["pick_type"],
        "commitment": row["commitment"],
        "role": row["role"],
        "confidence_probability": row["confidence_probability"],
        "confidence_label": row["confidence_label"],
        "price_at_pick": row["price_at_pick"],
        "betonline_spread_at_pick": _spread_dict(row),
        "my_number": _my_number(row),
        "reasoning": row["reasoning"],
        "uncertainties": json.loads(row["uncertainties_json"]),
        "result": row["result"],
    }
    if detailed:
        decision.update(
            selection=row["selection"],
            line=row["line"],
            line_source=row["line_source"],
            logged_after_start=bool(row["logged_after_start"]),
            closing_line=row["closing_line"],
            closing_price=row["closing_price"],
            closing_value=row["closing_value"],
            review=(
                {
                    "process_score": row["process_score"],
                    "luck_summary": row["luck_summary"],
                    "what_worked": row["what_worked"],
                    "what_to_improve": row["what_to_improve"],
                    "bias_tags": json.loads(row["bias_tags_json"]),
                }
                if row["process_score"] is not None
                else None
            ),
        )
    return decision


def _my_number(pick: Any) -> dict[str, Any] | None:
    if pick["my_line_low"] is None:
        return None
    return {"low": pick["my_line_low"], "high": pick["my_line_high"]}


def _describe_side(pick: dict[str, Any]) -> str:
    if pick["pick_type"] == "winner":
        return f"{pick['selection']} to win"
    if pick["line"] is None:
        return f"{pick['selection']} {pick['pick_type']}"
    if pick["pick_type"] == "spread":
        return f"{pick['selection']} {pick['line']:+g}"
    return f"{pick['selection']} {pick['line']:g}"


def _record(counter: Counter) -> str:
    return f"{counter['win']}-{counter['loss']}-{counter['push']}"
