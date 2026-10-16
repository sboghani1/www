from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger


def next_run(cron: str, timezone: str, after: datetime | None = None) -> datetime:
    zone = ZoneInfo(timezone)
    trigger = CronTrigger.from_crontab(cron, timezone=zone)
    base = after or datetime.now(UTC)
    result = trigger.get_next_fire_time(None, base.astimezone(zone))
    if result is None:
        raise ValueError("Cron expression has no future occurrence")
    return result.astimezone(UTC)
