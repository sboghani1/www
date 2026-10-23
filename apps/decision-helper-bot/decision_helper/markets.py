from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import MarketEvent, MarketQuote, parse_timestamp, team_matches


# The forwarder's last pre-tip BetOnline poll lands in a game's final 12
# minutes; reading the sheet this long after tip captures that close.
CLOSE_READ_DELAY = timedelta(minutes=15)


def quote_for_pick(
    event: MarketEvent,
    pick_type: str,
    selection: str,
) -> MarketQuote | None:
    return event.markets.get(f"{pick_type}:{selection}")


def spread_for_pick(
    event: MarketEvent,
    pick_type: str,
    selection: str,
) -> tuple[str, float] | None:
    """BetOnline spread as (team, point): the picked team's for a side, the
    favorite's for a total."""
    teams = (
        [selection]
        if pick_type in {"winner", "spread"}
        else [event.away_team, event.home_team]
    )
    priced = [
        (team, quote.point)
        for team in teams
        if (quote := event.markets.get(f"spread:{team}")) is not None
        and quote.point is not None
    ]
    return min(priced, key=lambda item: item[1], default=None)


def match_event(
    events: list[MarketEvent],
    *,
    event_id: str,
    away_team: str,
    home_team: str,
    commence_time: str,
) -> MarketEvent | None:
    exact = next((event for event in events if event.id == event_id), None)
    if exact is not None:
        return exact
    target_time = parse_timestamp(commence_time)
    candidates = [
        event
        for event in events
        if team_matches(event.away_team, away_team)
        and team_matches(event.home_team, home_team)
        and abs(parse_timestamp(event.commence_time) - target_time)
        <= timedelta(hours=3)
    ]
    return min(
        candidates,
        key=lambda event: abs(parse_timestamp(event.commence_time) - target_time),
        default=None,
    )


def next_market_check(
    commence_time: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Next sheet read: 24h/6h/1h before tip, then once after tip (the close)."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    commence = parse_timestamp(commence_time)
    close_read = commence + CLOSE_READ_DELAY
    if current >= close_read:
        return None
    for offset in (
        timedelta(hours=24),
        timedelta(hours=6),
        timedelta(hours=1),
    ):
        due = commence - offset
        if due > current:
            return due.isoformat().replace("+00:00", "Z")
    return close_read.isoformat().replace("+00:00", "Z")
