from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import parse_timestamp, utc_timestamp

NEAR_TIP_THRESHOLD = timedelta(hours=6)
FAR_INTERVAL = timedelta(hours=1)
NEAR_INTERVAL = timedelta(minutes=15)


def poll_interval(
    commence_time_utc: str,
    now: datetime,
) -> timedelta | None:
    until_tip = parse_timestamp(commence_time_utc) - now
    if until_tip.total_seconds() <= 0:
        return None
    if until_tip > NEAR_TIP_THRESHOLD:
        return FAR_INTERVAL
    return NEAR_INTERVAL


def is_due(record: dict[str, Any], now: datetime) -> bool:
    commence = str(record.get("commence_time_utc") or "")
    if not commence:
        return False
    interval = poll_interval(commence, now)
    if interval is None:
        return False
    last_poll = str(record.get("last_odds_polled_at") or "")
    if not last_poll:
        return True
    return now - parse_timestamp(last_poll) >= interval


def due_games(
    records: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    return [record for record in records if is_due(record, now)]


def next_poll_timestamp(
    commence_time_utc: str,
    now: datetime,
) -> str:
    interval = poll_interval(commence_time_utc, now)
    if interval is None:
        return ""
    commence = parse_timestamp(commence_time_utc)
    return utc_timestamp(min(now + interval, commence))
