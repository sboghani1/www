from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from .models import (
    ET,
    ScheduleGame,
    eastern_timestamp,
    parse_timestamp,
    utc_timestamp,
)

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/"
    "basketball/wnba/scoreboard"
)


def rolling_date_range(
    now: datetime,
    *,
    days: int = 14,
) -> tuple[str, str]:
    if days <= 0:
        raise ValueError("days must be positive")
    start = now.astimezone(ET).date()
    end = start + timedelta(days=days - 1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _status(event: dict[str, Any]) -> str:
    status_type = event.get("status", {}).get("type", {})
    if status_type.get("completed"):
        return "final"
    state = str(status_type.get("state", "")).lower()
    if state == "in":
        return "in_progress"
    if state == "post":
        return "final"
    if state == "pre":
        return "scheduled"
    name = str(status_type.get("name", "")).lower()
    return name.removeprefix("status_") or "unknown"


def _venue(competition: dict[str, Any]) -> str:
    venue = competition.get("venue") or {}
    parts = [str(venue.get("fullName") or venue.get("shortName") or "")]
    address = venue.get("address") or {}
    city = str(address.get("city") or "")
    state = str(address.get("state") or "")
    locality = ", ".join(part for part in (city, state) if part)
    if locality:
        parts.append(locality)
    return ", ".join(part for part in parts if part)


def _broadcast(competition: dict[str, Any]) -> str:
    names: list[str] = []
    for item in competition.get("broadcasts") or []:
        for name in item.get("names") or []:
            value = str(name)
            if value and value not in names:
                names.append(value)
    return ", ".join(names)


def parse_schedule(payload: dict[str, Any]) -> list[ScheduleGame]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("ESPN response is missing an events list")

    games: list[ScheduleGame] = []
    malformed = 0
    for event in events:
        try:
            competitions = event.get("competitions") or []
            if not competitions:
                raise ValueError("missing competition")
            competition = competitions[0]
            competitors = competition.get("competitors") or []
            if len(competitors) != 2:
                raise ValueError("expected two competitors")

            teams: dict[str, str] = {}
            for competitor in competitors:
                side = str(competitor.get("homeAway") or "")
                team = competitor.get("team") or {}
                name = str(
                    team.get("displayName") or team.get("name") or ""
                )
                if side in {"away", "home"} and name:
                    teams[side] = name
            if set(teams) != {"away", "home"}:
                raise ValueError("missing home or away team")

            commence = parse_timestamp(str(event["date"]))
            event_id = str(event["id"])
            if not event_id:
                raise ValueError("missing event id")
            games.append(
                ScheduleGame(
                    espn_event_id=event_id,
                    status=_status(event),
                    commence_time_utc=utc_timestamp(commence),
                    commence_time_et=eastern_timestamp(commence),
                    away_team=teams["away"],
                    home_team=teams["home"],
                    venue=_venue(competition),
                    broadcast=_broadcast(competition),
                )
            )
        except (KeyError, TypeError, ValueError):
            malformed += 1

    if events and malformed == len(events):
        raise ValueError("ESPN response contained no parseable WNBA events")
    return sorted(games, key=lambda game: game.commence_time_utc)


def fetch_schedule(
    *,
    now: datetime,
    days: int = 14,
    client: httpx.Client | None = None,
    timeout: float = 20,
) -> list[ScheduleGame]:
    start, end = rolling_date_range(now, days=days)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        try:
            response = client.get(
                ESPN_SCOREBOARD_URL,
                params={"dates": f"{start}-{end}", "limit": "500"},
                headers={"User-Agent": "wnba-poller/0.1"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("ESPN schedule request failed") from exc
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"ESPN schedule request returned HTTP {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("ESPN response must be a JSON object")
        return parse_schedule(payload)
    finally:
        if owns_client:
            client.close()
