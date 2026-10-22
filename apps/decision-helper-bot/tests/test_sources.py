import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from decision_helper.espn import EspnClient, match_final, parse_finals, parse_started_events
from decision_helper.markets import (
    OddsClient,
    match_event,
    next_market_check,
    parse_events,
)


def odds_payload() -> list:
    def book(spread: float, total: float, price: int) -> dict:
        return {
            "key": f"book{spread}",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Buffalo Bills", "price": price},
                    {"name": "Kansas City Chiefs", "price": -price - 20},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": "Buffalo Bills", "point": spread, "price": -110},
                    {"name": "Kansas City Chiefs", "point": -spread, "price": -110},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total, "price": -110},
                    {"name": "Under", "point": total, "price": -110},
                ]},
            ],
        }

    return [
        {
            "id": "evt",
            "commence_time": "2026-10-25T20:25:00Z",
            "away_team": "Buffalo Bills",
            "home_team": "Kansas City Chiefs",
            "bookmakers": [book(2.5, 47.5, 120), book(3.0, 48.0, 125), book(2.5, 47.0, 118)],
        },
        {"id": "broken"},
    ]


def test_consensus_uses_median_across_books() -> None:
    [event] = parse_events(odds_payload(), "nfl")
    assert event.markets["spread:Buffalo Bills"].point == 2.5
    assert event.markets["spread:Kansas City Chiefs"].point == -2.5
    assert event.markets["total:Over"].point == 47.5
    assert event.markets["winner:Buffalo Bills"].price == 120
    assert event.markets["winner:Buffalo Bills"].bookmaker_count == 3


def test_odds_client_requests_nba_and_nfl_keys() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.params["markets"] == "h2h,spreads,totals"
        return httpx.Response(200, json=odds_payload())

    client = OddsClient("key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(client.fetch("nfl"))
    asyncio.run(client.fetch("nba"))
    assert seen == [
        "/v4/sports/americanfootball_nfl/odds/",
        "/v4/sports/basketball_nba/odds/",
    ]


def test_odds_client_surfaces_http_errors() -> None:
    client = OddsClient(
        "key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        asyncio.run(client.fetch("nba"))


def test_match_event_falls_back_to_teams_and_time() -> None:
    events = parse_events(odds_payload(), "nfl")
    assert match_event(
        events, event_id="other", away_team="Buffalo Bills",
        home_team="Kansas City Chiefs", commence_time="2026-10-25T21:00:00Z",
    ) is events[0]


def test_market_checks_step_toward_kickoff() -> None:
    kickoff = "2026-10-25T20:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 20, tzinfo=UTC)) == "2026-10-24T20:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 15, tzinfo=UTC)) == "2026-10-25T19:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 19, 30, tzinfo=UTC)) == "2026-10-25T19:50:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 21, tzinfo=UTC)) is None


def espn_payload() -> dict:
    return {
        "events": [
            {
                "id": "401",
                "date": "2026-10-25T20:25Z",
                "status": {"type": {"completed": True, "state": "post"}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": "27", "team": {"displayName": "Kansas City Chiefs"}},
                    {"homeAway": "away", "score": "24", "team": {"displayName": "Buffalo Bills"}},
                ]}],
            },
            {
                "id": "402",
                "date": "2026-10-25T23:00Z",
                "status": {"type": {"completed": False, "state": "in"}},
                "competitions": [{"competitors": []}],
            },
        ]
    }


def test_parse_finals_only_returns_completed_games() -> None:
    finals = parse_finals(espn_payload(), "nfl")
    assert [(game.id, game.away_score, game.home_score) for game in finals] == [("401", 24, 27)]
    assert match_final(
        finals, away_team="Buffalo Bills", home_team="Kansas City Chiefs",
        commence_time="2026-10-25T20:25:00Z",
    ).id == "401"


def test_parse_started_events_includes_live_and_final_games_only() -> None:
    payload = espn_payload()
    payload["events"][1]["competitions"][0]["competitors"] = [
        {"homeAway": "home", "score": "50", "team": {"displayName": "Chicago Bulls"}},
        {"homeAway": "away", "score": "48", "team": {"displayName": "Detroit Pistons"}},
    ]
    payload["events"].append(
        {**payload["events"][0], "id": "403", "status": {"type": {"state": "pre"}}}
    )
    events = parse_started_events(payload, "nba")
    assert [(event.id, event.away_team, event.home_team, event.markets) for event in events] == [
        ("espn-401", "Buffalo Bills", "Kansas City Chiefs", {}),
        ("espn-402", "Detroit Pistons", "Chicago Bulls", {}),
    ]


def test_espn_client_uses_league_scoreboard() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.params["dates"]))
        return httpx.Response(200, json=espn_payload())

    client = EspnClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(client.fetch_finals("nba", "20261025"))
    assert seen == [("/apis/site/v2/sports/basketball/nba/scoreboard", "20261025")]
