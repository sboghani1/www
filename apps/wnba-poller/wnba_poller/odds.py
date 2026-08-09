from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx

from .models import (
    OddsLines,
    eastern_timestamp,
    parse_timestamp,
    utc_timestamp,
)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_wnba"
BOOKMAKER_KEY = "betonlineag"
FULL_MARKETS = "h2h,spreads,totals"
FIRST_HALF_MARKETS = "spreads_h1,totals_h1"


@dataclass(frozen=True)
class OddsFetchResult:
    lines: list[OddsLines]
    requests_used: str
    requests_remaining: str
    used_fallback: bool = False
    primary_failure: str = ""


def _normalized_team(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _find_outcome(
    outcomes: list[dict[str, Any]],
    name: str,
) -> tuple[float | None, int | None]:
    for outcome in outcomes:
        if outcome.get("name") != name:
            continue
        point = outcome.get("point")
        price = outcome.get("price")
        return (
            float(point) if point is not None else None,
            int(price) if price is not None else None,
        )
    return None, None


def _betonline(game: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            bookmaker
            for bookmaker in game.get("bookmakers") or []
            if bookmaker.get("key") == BOOKMAKER_KEY
        ),
        None,
    )


def merge_first_half(
    game: dict[str, Any],
    period_payload: dict[str, Any],
) -> None:
    source = _betonline(period_payload)
    if source is None:
        return
    target = _betonline(game)
    if target is None:
        game.setdefault("bookmakers", []).append(source)
        return
    markets = {
        str(market.get("key")): market
        for market in target.get("markets") or []
    }
    for market in source.get("markets") or []:
        markets[str(market.get("key"))] = market
    target["markets"] = list(markets.values())


def parse_game(
    game: dict[str, Any],
    *,
    captured_at: datetime,
    espn_event_id: str = "",
    requests_used: str = "",
    requests_remaining: str = "",
) -> OddsLines | None:
    bookmaker = _betonline(game)
    if bookmaker is None:
        return None
    markets = {
        str(market.get("key")): market.get("outcomes") or []
        for market in bookmaker.get("markets") or []
    }
    away = str(game.get("away_team") or "")
    home = str(game.get("home_team") or "")
    if not away or not home:
        raise ValueError("Odds API game is missing team names")
    commence = parse_timestamp(str(game["commence_time"]))

    away_spread, away_spread_price = _find_outcome(
        markets.get("spreads", []), away
    )
    home_spread, home_spread_price = _find_outcome(
        markets.get("spreads", []), home
    )
    _, away_moneyline = _find_outcome(markets.get("h2h", []), away)
    _, home_moneyline = _find_outcome(markets.get("h2h", []), home)
    total, over_price = _find_outcome(markets.get("totals", []), "Over")
    under_total, under_price = _find_outcome(
        markets.get("totals", []), "Under"
    )
    if total is None:
        total = under_total

    first_half_away_spread, first_half_away_spread_price = _find_outcome(
        markets.get("spreads_h1", []), away
    )
    first_half_home_spread, first_half_home_spread_price = _find_outcome(
        markets.get("spreads_h1", []), home
    )
    first_half_total, first_half_over_price = _find_outcome(
        markets.get("totals_h1", []), "Over"
    )
    first_half_under_total, first_half_under_price = _find_outcome(
        markets.get("totals_h1", []), "Under"
    )
    if first_half_total is None:
        first_half_total = first_half_under_total

    return OddsLines(
        captured_at_utc=utc_timestamp(captured_at),
        event_id=str(game["id"]),
        espn_event_id=espn_event_id,
        commence_time_utc=utc_timestamp(commence),
        commence_time_et=eastern_timestamp(commence),
        away_team=away,
        home_team=home,
        bookmaker=str(bookmaker.get("title") or "BetOnline.ag"),
        away_spread=away_spread,
        away_spread_price=away_spread_price,
        away_moneyline=away_moneyline,
        home_spread=home_spread,
        home_spread_price=home_spread_price,
        home_moneyline=home_moneyline,
        total=total,
        over_price=over_price,
        under_price=under_price,
        first_half_away_spread=first_half_away_spread,
        first_half_away_spread_price=first_half_away_spread_price,
        first_half_home_spread=first_half_home_spread,
        first_half_home_spread_price=first_half_home_spread_price,
        first_half_total=first_half_total,
        first_half_over_price=first_half_over_price,
        first_half_under_price=first_half_under_price,
        api_requests_used=requests_used,
        api_requests_remaining=requests_remaining,
    )


def match_game(
    record: dict[str, Any],
    games: list[dict[str, Any]],
) -> dict[str, Any] | None:
    event_id = str(record.get("event_id") or "")
    if event_id:
        exact = next(
            (game for game in games if str(game.get("id")) == event_id),
            None,
        )
        if exact is not None:
            return exact

    away = _normalized_team(str(record.get("away_team") or ""))
    home = _normalized_team(str(record.get("home_team") or ""))
    commence_value = str(record.get("commence_time_utc") or "")
    if not away or not home or not commence_value:
        return None
    commence = parse_timestamp(commence_value)
    candidates = []
    for game in games:
        if (
            _normalized_team(str(game.get("away_team") or "")) != away
            or _normalized_team(str(game.get("home_team") or "")) != home
        ):
            continue
        difference = abs(
            parse_timestamp(str(game["commence_time"])) - commence
        )
        if difference <= timedelta(hours=2):
            candidates.append((difference, game))
    return min(candidates, default=(None, None), key=lambda item: item[0])[1]


class OddsClient:
    def __init__(
        self,
        api_key: str,
        *,
        fallback_api_key: str = "",
        on_primary_unavailable: Callable[[str], None] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 20,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is required")
        self._api_key = api_key
        self._fallback_api_key = fallback_api_key
        self._on_primary_unavailable = on_primary_unavailable
        self._used_fallback = False
        self._primary_failure = ""
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._retries = retries
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(
        self,
        path: str,
        *,
        markets: str,
    ) -> Any:
        params = {
            "apiKey": self._api_key,
            "bookmakers": BOOKMAKER_KEY,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        for attempt in range(self._retries + 1):
            try:
                response = self._client.get(
                    f"{ODDS_API_BASE}{path}",
                    params=params,
                )
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise RuntimeError("The Odds API request failed") from exc
                self._sleep(2**attempt)
                continue
            if 200 <= response.status_code < 300:
                return response
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._retries:
                self._sleep(2**attempt)
                continue
            if (
                not self._used_fallback
                and response.status_code in {401, 403, 429}
            ):
                self._primary_failure = f"HTTP {response.status_code}"
                if self._on_primary_unavailable is not None:
                    self._on_primary_unavailable(self._primary_failure)
                if self._fallback_api_key:
                    self._api_key = self._fallback_api_key
                    self._used_fallback = True
                    return self._get(path, markets=markets)
            raise RuntimeError(
                f"The Odds API returned HTTP {response.status_code}"
            )
        raise RuntimeError("The Odds API request failed")

    def fetch_due(
        self,
        due_records: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> OddsFetchResult:
        if not due_records:
            return OddsFetchResult([], "", "")

        bulk_response = self._get(
            f"/sports/{SPORT_KEY}/odds/",
            markets=FULL_MARKETS,
        )
        raw_games = bulk_response.json()
        if not isinstance(raw_games, list):
            raise ValueError("The Odds API bulk response must be a list")
        used = bulk_response.headers.get("x-requests-used", "")
        remaining = bulk_response.headers.get("x-requests-remaining", "")

        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_event_ids: set[str] = set()
        for record in due_records:
            game = match_game(record, raw_games)
            if game is None or _betonline(game) is None:
                continue
            event_id = str(game.get("id") or "")
            if not event_id or event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            matched.append((record, game))

        lines: list[OddsLines] = []
        for record, game in matched:
            event_id = str(game["id"])
            period_response = self._get(
                f"/sports/{SPORT_KEY}/events/{event_id}/odds",
                markets=FIRST_HALF_MARKETS,
            )
            period_payload = period_response.json()
            if not isinstance(period_payload, dict):
                raise ValueError(
                    "The Odds API event response must be an object"
                )
            merge_first_half(game, period_payload)
            used = period_response.headers.get("x-requests-used", used)
            remaining = period_response.headers.get(
                "x-requests-remaining", remaining
            )
            parsed = parse_game(
                game,
                captured_at=now,
                espn_event_id=str(record.get("espn_event_id") or ""),
                requests_used=used,
                requests_remaining=remaining,
            )
            if parsed is not None:
                lines.append(parsed)

        return OddsFetchResult(
            lines=sorted(
                lines, key=lambda line: line.commence_time_utc
            ),
            requests_used=used,
            requests_remaining=remaining,
            used_fallback=self._used_fallback,
            primary_failure=self._primary_failure,
        )
