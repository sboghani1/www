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

from .markets import next_market_check, quote_for_pick
from .models import (
    DecisionReview,
    Extraction,
    FinalGame,
    MarketEvent,
    closing_value,
    grade_pick,
    json_array,
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
                    logged_after_start INTEGER NOT NULL DEFAULT 0
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
                """
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
    ) -> list[dict[str, Any]]:
        by_id = {event.id: event for event in events}
        now_dt = utc_now()
        now = timestamp(now_dt)
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
                quote = quote_for_pick(event, pick.pick_type, pick.selection)
                line = pick.line
                line_source = "spoken" if line is not None else "none"
                if line is None and quote is not None and quote.point is not None:
                    line = quote.point
                    line_source = "market"
                price = quote.price if quote is not None else None
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
                        commitment, ordinal, logged_after_start
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'open', ?, ?, ?, ?, ?, 'not_ready', ?, ?, ?, ?
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
                        now if quote else None,
                        next_market_check(event.commence_time, now=now_dt),
                        timestamp(commence + result_delay),
                        now,
                        pick.commitment,
                        ordinal,
                        int(commence <= now_dt),
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
                            now,
                            quote.point,
                            quote.price,
                            quote.bookmaker_count,
                        ),
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
            else:
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
            clv = closing_value(
                pick_type=row["pick_type"],
                selection=row["selection"],
                pick_line=row["line"],
                closing_line=row["closing_line"],
                pick_price=row["price_at_pick"],
                closing_price=row["closing_price"],
            )
            connection.execute(
                """
                UPDATE picks
                SET status='final', result=?, away_score=?, home_score=?,
                    closing_value=?, analysis_status='pending',
                    next_analysis_at=?, settled_at=?,
                    next_market_check_at=NULL
                WHERE id=?
                """,
                (
                    result,
                    game.away_score,
                    game.home_score,
                    clv,
                    now,
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

    def mark_feedback_sent(self, pick_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE picks SET feedback_sent_at=? WHERE id=?",
                (timestamp(utc_now()), pick_id),
            )
            connection.commit()

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

    def reopen_entry(self, entry_id: str) -> dict[str, Any]:
        """Void an entry's open picks so its saved transcript can be reparsed."""
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
            settled = connection.execute(
                "SELECT COUNT(*) FROM picks WHERE entry_id=? AND status='final'",
                (entry_id,),
            ).fetchone()[0]
            if settled:
                raise ValueError("That note already has settled picks")
            connection.execute(
                """
                UPDATE picks
                SET status='void', next_market_check_at=NULL
                WHERE entry_id=? AND status='open'
                """,
                (entry_id,),
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

    def stats(self, commitment: str = "pick") -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM picks WHERE status!='void' AND commitment=?",
                (commitment,),
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
