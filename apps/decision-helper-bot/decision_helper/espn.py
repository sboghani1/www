from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from .models import FinalGame, MarketEvent, parse_timestamp, team_matches


SCOREBOARD_URLS = {
    "nfl": (
        "https://site.api.espn.com/apis/site/v2/sports/"
        "football/nfl/scoreboard"
    ),
    "nba": (
        "https://site.api.espn.com/apis/site/v2/sports/"
        "basketball/nba/scoreboard"
    ),
}


def parse_started_events(payload: Any, sport: str) -> list[MarketEvent]:
    """Games that are live or final, offered as candidates for late-logged picks."""
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError("ESPN response is missing an events list")
    events: list[MarketEvent] = []
    for event in payload["events"]:
        try:
            state = str(event["status"]["type"]["state"]).casefold()
            if state not in {"in", "post"}:
                continue
            teams = {
                str(competitor["homeAway"]): str(competitor["team"]["displayName"])
                for competitor in event["competitions"][0]["competitors"]
            }
            events.append(
                MarketEvent(
                    id=f"espn-{event['id']}",
                    sport=sport,
                    commence_time=str(event["date"]),
                    away_team=teams["away"],
                    home_team=teams["home"],
                    markets={},
                )
            )
        except (IndexError, KeyError, TypeError):
            continue
    return events


def parse_finals(payload: Any, sport: str) -> list[FinalGame]:
    if sport not in SCOREBOARD_URLS:
        raise ValueError("sport must be nfl or nba")
    if not isinstance(payload, dict) or not isinstance(
        payload.get("events"), list
    ):
        raise ValueError("ESPN response is missing an events list")
    finals: list[FinalGame] = []
    malformed = 0
    for event in payload["events"]:
        try:
            status = event.get("status", {}).get("type", {})
            if not (
                status.get("completed")
                or str(status.get("state", "")).casefold() == "post"
            ):
                continue
            competition = (event.get("competitions") or [])[0]
            competitors = competition.get("competitors") or []
            if len(competitors) != 2:
                raise ValueError("expected two competitors")
            teams: dict[str, str] = {}
            scores: dict[str, int] = {}
            for competitor in competitors:
                side = str(competitor.get("homeAway") or "")
                team = competitor.get("team") or {}
                name = str(
                    team.get("displayName") or team.get("name") or ""
                )
                score = int(float(str(competitor["score"])))
                if side in {"away", "home"} and name:
                    teams[side] = name
                    scores[side] = score
            if set(teams) != {"away", "home"} or set(scores) != {
                "away",
                "home",
            }:
                raise ValueError("final score is incomplete")
            finals.append(
                FinalGame(
                    id=str(event["id"]),
                    sport=sport,
                    commence_time=str(event["date"]),
                    away_team=teams["away"],
                    home_team=teams["home"],
                    away_score=scores["away"],
                    home_score=scores["home"],
                )
            )
        except (IndexError, KeyError, TypeError, ValueError):
            malformed += 1
    if payload["events"] and malformed == len(payload["events"]):
        raise ValueError("ESPN response contained no parseable events")
    return finals


def match_final(
    finals: list[FinalGame],
    *,
    away_team: str,
    home_team: str,
    commence_time: str,
) -> FinalGame | None:
    target = parse_timestamp(commence_time)
    candidates = [
        game
        for game in finals
        if team_matches(game.away_team, away_team)
        and team_matches(game.home_team, home_team)
        and abs(parse_timestamp(game.commence_time) - target)
        <= timedelta(hours=6)
    ]
    return min(
        candidates,
        key=lambda game: abs(parse_timestamp(game.commence_time) - target),
        default=None,
    )


class EspnClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch_finals(self, sport: str, date: str) -> list[FinalGame]:
        return parse_finals(await self._scoreboard(sport, date), sport)

    async def fetch_started(self, sport: str, date: str) -> list[MarketEvent]:
        return parse_started_events(await self._scoreboard(sport, date), sport)

    async def _scoreboard(self, sport: str, date: str) -> Any:
        url = SCOREBOARD_URLS.get(sport)
        if url is None:
            raise ValueError("sport must be nfl or nba")
        try:
            response = await self.client.get(
                url,
                params={"dates": date, "limit": "100"},
                headers={"User-Agent": "decision-helper-bot/0.1"},
            )
        except httpx.HTTPError as error:
            raise RuntimeError("ESPN scoreboard request failed") from error
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"ESPN scoreboard returned HTTP {response.status_code}"
            )
        return response.json()
