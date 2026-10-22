import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from decision_helper.espn import EspnClient, match_final, parse_finals, parse_started_events
from decision_helper.lines import SheetLines, parse_rows, parse_snapshots
from decision_helper.markets import match_event, next_market_check


AWAY = "latest_away_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline"
HOME = AWAY.replace("latest_away_", "latest_home_")
TOTALS = "latest_totals_game_total_overprice_underprice__h1_total_overprice_underprice__q1_total_overprice_underprice"


def sheet_rows() -> list[list[str]]:
    return [
        ["event_id", "commence_time_utc", "away_team", "home_team", "opening_away_x", AWAY, HOME, TOTALS],
        ["evt", "2026-10-25T20:25:00Z", "Buffalo Bills", "Kansas City Chiefs", "9,9,9",
         "2.5,-110,120|nodata,nodata,nodata|nodata,nodata,nodata",
         "-2.5,-110,-140|nodata,nodata,nodata|nodata,nodata,nodata",
         "47.5,-108,-112|nodata,nodata,nodata|nodata,nodata,nodata"],
        ["nolines", "2026-10-25T23:00:00Z", "Los Angeles Clippers", "Utah Jazz", "",
         "nodata,nodata,nodata", "nodata,nodata,nodata", "nodata,nodata,nodata"],
        ["bad", "not-a-time", "A", "B", "", "", "", ""],
    ]


def test_sheet_rows_decode_latest_betonline_game_lines() -> None:
    event, empty = parse_rows(sheet_rows(), "nfl")
    assert event.id == "evt" and event.sport == "nfl"
    assert event.markets["spread:Buffalo Bills"].point == 2.5
    assert event.markets["spread:Kansas City Chiefs"].price == -110
    assert event.markets["winner:Buffalo Bills"].price == 120
    assert event.markets["winner:Buffalo Bills"].point is None
    assert event.markets["total:Over"].point == 47.5
    assert event.markets["total:Under"].price == -112
    assert event.markets["total:Over"].bookmaker_count == 1
    assert empty.id == "nolines" and empty.markets == {}


def test_sheet_lines_reads_sport_tab_and_caches() -> None:
    tabs = []

    def loader(tab: str):
        tabs.append(tab)
        return sheet_rows()

    lines = SheetLines(loader=loader)
    asyncio.run(lines.fetch("nba"))
    asyncio.run(lines.fetch("nba"))
    asyncio.run(lines.fetch("nfl"))
    assert tabs == ["nba_games", "nfl_games"]


def snapshot_rows() -> list[list[str]]:
    away = AWAY.removeprefix("latest_")
    home = HOME.removeprefix("latest_")
    totals = TOTALS.removeprefix("latest_")
    header = ["captured_at", "event_id", "commence_time_utc", "away_team", "home_team", "bookmaker", away, home, totals]

    def row(at, spread, total):
        return [at, "evt", "2026-10-23T00:15:00Z", "New England Patriots", "Chicago Bears", "BetOnline",
                f"{spread},-110,130|nodata,nodata,nodata", f"{-spread},-110,-150|nodata,nodata,nodata",
                f"{total},-110,-110"]

    return [header, row("2026-10-21T18:00:00Z", 2.5, 44.5), row("2026-10-22T05:00:00Z", 3.0, 45.0),
            row("2026-10-22T15:00:00Z", 3.5, 45.5), ["bad-time", "evt"]]


def test_snapshots_give_the_line_at_note_time() -> None:
    (event,) = parse_snapshots(snapshot_rows(), "nfl", datetime(2026, 10, 22, 6, 25, tzinfo=UTC))
    assert event.markets["spread:New England Patriots"].point == 3.0
    assert event.markets["spread:Chicago Bears"].point == -3.0
    assert event.markets["total:Under"].point == 45.0
    assert parse_snapshots(snapshot_rows(), "nfl", datetime(2026, 10, 20, tzinfo=UTC)) == []

    tabs = []

    def loader(tab):
        tabs.append(tab)
        return snapshot_rows()

    lines = SheetLines(loader=loader)
    (late,) = asyncio.run(lines.fetch("nfl", as_of=datetime(2026, 10, 22, 16, tzinfo=UTC)))
    assert late.markets["spread:New England Patriots"].point == 3.5
    assert tabs == ["nfl_line_snapshots"]


def test_sheet_lines_surfaces_read_errors() -> None:
    def loader(tab: str):
        raise OSError("quota")

    with pytest.raises(RuntimeError, match="sheet read failed"):
        asyncio.run(SheetLines(loader=loader).fetch("nba"))


def test_match_event_falls_back_to_teams_and_time() -> None:
    events = parse_rows(sheet_rows(), "nfl")
    assert match_event(
        events, event_id="other", away_team="Buffalo Bills",
        home_team="Kansas City Chiefs", commence_time="2026-10-25T21:00:00Z",
    ) is events[0]


def test_market_checks_step_toward_kickoff_then_read_the_close() -> None:
    kickoff = "2026-10-25T20:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 20, tzinfo=UTC)) == "2026-10-24T20:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 15, tzinfo=UTC)) == "2026-10-25T19:00:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 19, 30, tzinfo=UTC)) == "2026-10-25T20:15:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 20, 5, tzinfo=UTC)) == "2026-10-25T20:15:00Z"
    assert next_market_check(kickoff, now=datetime(2026, 10, 25, 20, 15, tzinfo=UTC)) is None


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
