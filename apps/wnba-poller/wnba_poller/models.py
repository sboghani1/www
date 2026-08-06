from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NO_DATA = "nodata"

LINE_FIELDS = (
    "away_spread",
    "away_spread_price",
    "away_moneyline",
    "home_spread",
    "home_spread_price",
    "home_moneyline",
    "total",
    "over_price",
    "under_price",
    "first_half_away_spread",
    "first_half_away_spread_price",
    "first_half_home_spread",
    "first_half_home_spread_price",
    "first_half_total",
    "first_half_over_price",
    "first_half_under_price",
)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def eastern_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(ET).isoformat()


@dataclass(frozen=True)
class ScheduleGame:
    espn_event_id: str
    status: str
    commence_time_utc: str
    commence_time_et: str
    away_team: str
    home_team: str
    venue: str
    broadcast: str
    away_score: int | None = None
    home_score: int | None = None


@dataclass(frozen=True)
class OddsLines:
    captured_at_utc: str
    event_id: str
    espn_event_id: str
    commence_time_utc: str
    commence_time_et: str
    away_team: str
    home_team: str
    bookmaker: str
    away_spread: float | None
    away_spread_price: int | None
    away_moneyline: int | None
    home_spread: float | None
    home_spread_price: int | None
    home_moneyline: int | None
    total: float | None
    over_price: int | None
    under_price: int | None
    first_half_away_spread: float | None
    first_half_away_spread_price: int | None
    first_half_home_spread: float | None
    first_half_home_spread_price: int | None
    first_half_total: float | None
    first_half_over_price: int | None
    first_half_under_price: int | None
    api_requests_used: str
    api_requests_remaining: str

    def signature(self) -> tuple[Any, ...]:
        return tuple(getattr(self, field) for field in LINE_FIELDS)
