from copy import deepcopy
from datetime import datetime, timezone

from wnba_poller.odds import (
    FIRST_HALF_MARKETS,
    FULL_MARKETS,
    OddsClient,
    parse_game,
)

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def _full_game() -> dict:
    return {
        "id": "odds-1",
        "commence_time": "2026-08-05T23:00:00Z",
        "away_team": "Atlanta Dream",
        "home_team": "New York Liberty",
        "bookmakers": [
            {
                "key": "betonlineag",
                "title": "BetOnline.ag",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Atlanta Dream", "price": 150},
                            {"name": "New York Liberty", "price": -175},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {
                                "name": "Atlanta Dream",
                                "point": 4.5,
                                "price": -110,
                            },
                            {
                                "name": "New York Liberty",
                                "point": -4.5,
                                "price": -110,
                            },
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "point": 164.5, "price": -105},
                            {"name": "Under", "point": 164.5, "price": -115},
                        ],
                    },
                ],
            }
        ],
    }


def _period_payload() -> dict:
    return {
        "id": "odds-1",
        "bookmakers": [
            {
                "key": "betonlineag",
                "title": "BetOnline.ag",
                "markets": [
                    {
                        "key": "spreads_h1",
                        "outcomes": [
                            {
                                "name": "Atlanta Dream",
                                "point": 2.5,
                                "price": -112,
                            },
                            {
                                "name": "New York Liberty",
                                "point": -2.5,
                                "price": -108,
                            },
                        ],
                    },
                    {
                        "key": "totals_h1",
                        "outcomes": [
                            {"name": "Over", "point": 82.5, "price": -110},
                            {"name": "Under", "point": 82.5, "price": -110},
                        ],
                    },
                ],
            }
        ],
    }


def test_parse_full_game_and_first_half_markets() -> None:
    game = _full_game()
    game["bookmakers"][0]["markets"].extend(
        _period_payload()["bookmakers"][0]["markets"]
    )

    lines = parse_game(game, captured_at=NOW)

    assert lines is not None
    assert lines.away_moneyline == 150
    assert lines.home_spread == -4.5
    assert lines.total == 164.5
    assert lines.first_half_away_spread == 2.5
    assert lines.first_half_total == 82.5


def test_missing_first_half_is_nodata_in_model() -> None:
    lines = parse_game(_full_game(), captured_at=NOW)

    assert lines is not None
    assert lines.first_half_away_spread is None
    assert lines.first_half_total is None


def test_missing_betonline_skips_game() -> None:
    game = _full_game()
    game["bookmakers"][0]["key"] = "other"

    assert parse_game(game, captured_at=NOW) is None


class _Response:
    def __init__(self, payload: object, used: str) -> None:
        self.status_code = 200
        self._payload = payload
        self.headers = {
            "x-requests-used": used,
            "x-requests-remaining": str(500 - int(used)),
        }

    def json(self) -> object:
        return deepcopy(self._payload)


class _HTTPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict) -> _Response:
        self.calls.append((url, params))
        if "/events/" in url:
            return _Response(_period_payload(), "11")
        return _Response([_full_game()], "10")


def test_bulk_full_game_and_event_specific_first_half_requests() -> None:
    http = _HTTPClient()
    client = OddsClient("not-a-real-key", client=http)
    due = [
        {
            "espn_event_id": "espn-1",
            "event_id": "",
            "commence_time_utc": "2026-08-05T23:00:00Z",
            "away_team": "Atlanta Dream",
            "home_team": "New York Liberty",
        }
    ]

    result = client.fetch_due(due, now=NOW)

    assert len(http.calls) == 2
    assert http.calls[0][1]["markets"] == FULL_MARKETS
    assert http.calls[1][1]["markets"] == FIRST_HALF_MARKETS
    assert result.lines[0].espn_event_id == "espn-1"
    assert result.lines[0].first_half_total == 82.5
    assert result.requests_used == "11"
    assert result.requests_remaining == "489"
