from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .models import MarketEvent, MarketQuote, parse_timestamp, team_matches


ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
}


def _median_number(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _median_price(values: list[int]) -> int | None:
    return round(statistics.median(values)) if values else None


def _market_key(market: str, outcome: str) -> str:
    if market == "h2h":
        return f"winner:{outcome}"
    if market == "spreads":
        return f"spread:{outcome}"
    if market == "totals":
        return f"total:{outcome.title()}"
    raise ValueError(f"Unsupported market: {market}")


def parse_events(payload: Any, sport: str) -> list[MarketEvent]:
    if sport not in SPORT_KEYS:
        raise ValueError("sport must be nfl or nba")
    if not isinstance(payload, list):
        raise ValueError("The Odds API response must be an array")
    events: list[MarketEvent] = []
    malformed = 0
    for raw_event in payload:
        try:
            if not isinstance(raw_event, dict):
                raise ValueError("event must be an object")
            values: dict[str, dict[str, list[float | int]]] = defaultdict(
                lambda: {"points": [], "prices": []}
            )
            for bookmaker in raw_event.get("bookmakers") or []:
                for market in bookmaker.get("markets") or []:
                    market_name = str(market.get("key") or "")
                    if market_name not in {"h2h", "spreads", "totals"}:
                        continue
                    for outcome in market.get("outcomes") or []:
                        outcome_name = str(outcome.get("name") or "")
                        if not outcome_name:
                            continue
                        key = _market_key(market_name, outcome_name)
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if isinstance(point, (int, float)) and not isinstance(
                            point, bool
                        ):
                            values[key]["points"].append(float(point))
                        if isinstance(price, (int, float)) and not isinstance(
                            price, bool
                        ):
                            values[key]["prices"].append(int(price))
            markets = {
                key: MarketQuote(
                    point=_median_number(
                        [float(value) for value in quote["points"]]
                    ),
                    price=_median_price(
                        [int(value) for value in quote["prices"]]
                    ),
                    bookmaker_count=max(
                        len(quote["points"]), len(quote["prices"])
                    ),
                )
                for key, quote in values.items()
            }
            event_id = str(raw_event["id"])
            away_team = str(raw_event["away_team"])
            home_team = str(raw_event["home_team"])
            commence_time = str(raw_event["commence_time"])
            parse_timestamp(commence_time)
            if not event_id or not away_team or not home_team:
                raise ValueError("event identity is incomplete")
            events.append(
                MarketEvent(
                    id=event_id,
                    sport=sport,
                    commence_time=commence_time,
                    away_team=away_team,
                    home_team=home_team,
                    markets=markets,
                )
            )
        except (KeyError, TypeError, ValueError):
            malformed += 1
    if payload and malformed == len(payload):
        raise ValueError("The Odds API response contained no valid events")
    return sorted(events, key=lambda event: event.commence_time)


def quote_for_pick(
    event: MarketEvent,
    pick_type: str,
    selection: str,
) -> MarketQuote | None:
    return event.markets.get(f"{pick_type}:{selection}")


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


class OddsClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is required")
        self.api_key = api_key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch(self, sport: str) -> list[MarketEvent]:
        sport_key = SPORT_KEYS.get(sport)
        if sport_key is None:
            raise ValueError("sport must be nfl or nba")
        try:
            response = await self.client.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds/",
                params={
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": "h2h,spreads,totals",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
                headers={"User-Agent": "decision-helper-bot/0.1"},
            )
        except httpx.HTTPError as error:
            raise RuntimeError("The Odds API request failed") from error
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"The Odds API returned HTTP {response.status_code}"
            )
        return parse_events(response.json(), sport)


def next_market_check(
    commence_time: str,
    *,
    now: datetime | None = None,
) -> str | None:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    commence = parse_timestamp(commence_time)
    if commence <= current:
        return None
    for offset in (
        timedelta(hours=24),
        timedelta(hours=6),
        timedelta(hours=1),
        timedelta(minutes=10),
    ):
        due = commence - offset
        if due > current:
            return due.isoformat().replace("+00:00", "Z")
    return current.isoformat().replace("+00:00", "Z")
