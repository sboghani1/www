"""BetOnline lines from the forwarder's intake workbook (no Odds API credits).

telegram-channel-forwarder's nfl-lines-fetcher / nba-lines-fetcher keep one row
per game in ``nfl_games`` / ``nba_games``. The ``latest_*`` packed cells hold
the newest pre-tip BetOnline line, so once a game starts they are its close.
Packed cells look like ``spread,price,moneyline|h1...|q1...`` (sides) and
``total,over,under|...`` (totals), with ``nodata`` for missing values.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import Any

from .models import MarketEvent, MarketQuote, parse_timestamp


SHEET_TABS = {"nfl": "nfl_games", "nba": "nba_games"}
BOOKMAKER = "BetOnline"
CACHE_SECONDS = 60
Rows = list[list[str]]


def _game_numbers(value: str) -> tuple[float | int | None, ...]:
    group = (value or "").split("|", 1)[0]
    numbers: list[float | int | None] = []
    for raw in group.split(","):
        raw = raw.strip()
        if raw in ("", "nodata"):
            numbers.append(None)
            continue
        number = float(raw)
        numbers.append(int(number) if number.is_integer() else number)
    while len(numbers) < 3:
        numbers.append(None)
    return tuple(numbers[:3])


def _quote(point: Any, price: Any) -> MarketQuote | None:
    if point is None and price is None:
        return None
    return MarketQuote(
        point=float(point) if point is not None else None,
        price=int(price) if price is not None else None,
        bookmaker_count=1,
    )


def _column(header: list[str], prefix: str) -> int:
    for index, name in enumerate(header):
        if name.startswith(prefix):
            return index
    raise ValueError(f"lines tab is missing a {prefix}* column")


def parse_rows(rows: Rows, sport: str) -> list[MarketEvent]:
    """MarketEvents (BetOnline game markets) from a ``*_games`` tab."""
    if not rows:
        return []
    header = rows[0]
    index = {name: position for position, name in enumerate(header)}
    for required in ("event_id", "commence_time_utc", "away_team", "home_team"):
        if required not in index:
            raise ValueError(f"lines tab is missing {required}")
    away_column = _column(header, "latest_away_")
    home_column = _column(header, "latest_home_")
    totals_column = _column(header, "latest_totals_")
    events: list[MarketEvent] = []
    for row in rows[1:]:
        row = row + [""] * (len(header) - len(row))
        try:
            commence = row[index["commence_time_utc"]]
            parse_timestamp(commence)
            away = row[index["away_team"]].strip()
            home = row[index["home_team"]].strip()
            event_id = row[index["event_id"]].strip()
            if not (away and home and event_id):
                continue
            away_spread, away_spread_price, away_ml = _game_numbers(
                row[away_column]
            )
            home_spread, home_spread_price, home_ml = _game_numbers(
                row[home_column]
            )
            total, over_price, under_price = _game_numbers(row[totals_column])
        except (ValueError, IndexError):
            continue
        candidates = {
            f"winner:{away}": _quote(None, away_ml),
            f"winner:{home}": _quote(None, home_ml),
            f"spread:{away}": _quote(away_spread, away_spread_price),
            f"spread:{home}": _quote(home_spread, home_spread_price),
            "total:Over": _quote(total, over_price),
            "total:Under": _quote(total, under_price),
        }
        events.append(
            MarketEvent(
                id=event_id,
                sport=sport,
                commence_time=commence,
                away_team=away,
                home_team=home,
                markets={
                    key: quote
                    for key, quote in candidates.items()
                    if quote is not None
                },
            )
        )
    return sorted(events, key=lambda event: event.commence_time)


def _gspread_loader(credentials_b64: str, sheet_id: str) -> Callable[[str], Rows]:
    def load(tab: str) -> Rows:
        import gspread

        info = json.loads(base64.b64decode(credentials_b64).decode())
        client = gspread.service_account_from_dict(info)
        return client.open_by_key(sheet_id).worksheet(tab).get_all_values()

    return load


class SheetLines:
    """Reads the intake workbook; never calls a paid odds API."""

    def __init__(
        self,
        credentials_b64: str = "",
        sheet_id: str = "",
        *,
        loader: Callable[[str], Rows] | None = None,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        if loader is None:
            if not credentials_b64 or not sheet_id:
                raise ValueError(
                    "GOOGLE_CREDENTIALS and DECISION_HELPER_LINES_SHEET_ID "
                    "are required"
                )
            loader = _gspread_loader(credentials_b64, sheet_id)
        self._load = loader
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, list[MarketEvent]]] = {}

    async def fetch(self, sport: str) -> list[MarketEvent]:
        tab = SHEET_TABS.get(sport)
        if tab is None:
            raise ValueError("sport must be nfl or nba")
        cached = self._cache.get(sport)
        if cached is not None and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        try:
            rows = await asyncio.to_thread(self._load, tab)
        except Exception as error:
            raise RuntimeError(f"BetOnline lines sheet read failed: {error}") from error
        events = parse_rows(rows, sport)
        self._cache[sport] = (time.monotonic(), events)
        return events

    async def close(self) -> None:
        return None
