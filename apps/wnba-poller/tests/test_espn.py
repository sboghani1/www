from datetime import datetime, timezone

import pytest

from wnba_poller.espn import parse_schedule, rolling_date_range


def _payload() -> dict:
    return {
        "events": [
            {
                "id": "401",
                "date": "2026-07-15T23:00:00Z",
                "status": {
                    "type": {
                        "name": "STATUS_SCHEDULED",
                        "state": "pre",
                        "completed": False,
                    }
                },
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "New York Liberty"},
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Atlanta Dream"},
                            },
                        ],
                        "venue": {
                            "fullName": "Barclays Center",
                            "address": {
                                "city": "Brooklyn",
                                "state": "NY",
                            },
                        },
                        "broadcasts": [
                            {"names": ["ESPN"]},
                            {"names": ["ESPN", "WNBA League Pass"]},
                        ],
                    }
                ],
            }
        ]
    }


def test_parse_espn_event_and_eastern_time() -> None:
    games = parse_schedule(_payload())

    assert len(games) == 1
    game = games[0]
    assert game.espn_event_id == "401"
    assert game.status == "scheduled"
    assert game.commence_time_utc == "2026-07-15T23:00:00Z"
    assert game.commence_time_et == "2026-07-15T19:00:00-04:00"
    assert game.away_team == "Atlanta Dream"
    assert game.home_team == "New York Liberty"
    assert game.venue == "Barclays Center, Brooklyn, NY"
    assert game.broadcast == "ESPN, WNBA League Pass"


def test_rolling_range_uses_eastern_date_and_fourteen_days() -> None:
    now = datetime(2026, 8, 5, 2, 30, tzinfo=timezone.utc)

    assert rolling_date_range(now) == ("20260804", "20260817")


def test_changed_espn_shape_fails_visibly() -> None:
    with pytest.raises(ValueError, match="events list"):
        parse_schedule({"items": []})


def test_all_malformed_events_fail_instead_of_clearing_schedule() -> None:
    with pytest.raises(ValueError, match="no parseable"):
        parse_schedule({"events": [{"id": "broken"}]})


def _final_payload(*, away_score: str = "82", home_score: str = "96") -> dict:
    return {
        "events": [
            {
                "id": "402",
                "date": "2026-08-05T23:00:00Z",
                "status": {
                    "type": {
                        "name": "STATUS_FINAL",
                        "state": "post",
                        "completed": True,
                    }
                },
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Atlanta Dream"},
                                "score": home_score,
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Phoenix Mercury"},
                                "score": away_score,
                            },
                        ],
                    }
                ],
            }
        ]
    }


def test_final_game_captures_scores() -> None:
    games = parse_schedule(_final_payload())

    assert len(games) == 1
    game = games[0]
    assert game.status == "final"
    assert game.away_score == 82
    assert game.home_score == 96


def test_scheduled_game_has_no_score_even_if_espn_sends_zero() -> None:
    payload = _payload()
    for competitor in payload["events"][0]["competitions"][0]["competitors"]:
        competitor["score"] = "0"

    games = parse_schedule(payload)

    assert games[0].status == "scheduled"
    assert games[0].away_score is None
    assert games[0].home_score is None


def test_final_game_with_unparseable_score_leaves_it_none() -> None:
    games = parse_schedule(_final_payload(away_score="", home_score="TBD"))

    assert games[0].away_score is None
    assert games[0].home_score is None
