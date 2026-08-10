from datetime import datetime, timedelta, timezone

import pytest

from wnba_poller.odds import OddsFetchResult
from wnba_poller.scheduler import is_due, poll_interval
from wnba_poller.service import backfill_scores, poll_odds, sync_schedule

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def _record(
    *,
    hours_to_tip: float,
    last_poll: datetime | None,
) -> dict:
    return {
        "event_id": "event-1",
        "commence_time_utc": (
            NOW + timedelta(hours=hours_to_tip)
        ).isoformat().replace("+00:00", "Z"),
        "last_odds_polled_at": (
            last_poll.isoformat().replace("+00:00", "Z")
            if last_poll
            else ""
        ),
    }


def test_due_interval_switches_at_six_hour_boundary() -> None:
    assert poll_interval(
        _record(hours_to_tip=6.01, last_poll=None)["commence_time_utc"],
        NOW,
    ) == timedelta(hours=1)
    assert poll_interval(
        _record(hours_to_tip=6, last_poll=None)["commence_time_utc"],
        NOW,
    ) == timedelta(minutes=15)


def test_due_logic_and_no_post_tip_polling() -> None:
    assert is_due(
        _record(hours_to_tip=7, last_poll=NOW - timedelta(minutes=60)),
        NOW,
    )
    assert not is_due(
        _record(hours_to_tip=7, last_poll=NOW - timedelta(minutes=59)),
        NOW,
    )
    assert is_due(
        _record(hours_to_tip=6, last_poll=NOW - timedelta(minutes=15)),
        NOW,
    )
    assert not is_due(
        _record(hours_to_tip=-0.01, last_poll=None),
        NOW,
    )


class _NoDueStore:
    def read_games(self) -> list[dict]:
        return [_record(hours_to_tip=7, last_poll=NOW)]


def test_scheduled_poll_makes_no_api_call_when_nothing_is_due() -> None:
    called = False

    def factory() -> object:
        nonlocal called
        called = True
        raise AssertionError("client must not be created")

    outcome = poll_odds(_NoDueStore(), now=NOW, client_factory=factory)

    assert not outcome.api_called
    assert not called


class _DueStore:
    def read_games(self) -> list[dict]:
        return [_record(hours_to_tip=7, last_poll=None)]

    def persist_odds_poll(
        self,
        *,
        due_records: list[dict],
        lines: list,
        requests_used: str,
        requests_remaining: str,
        now: datetime,
    ) -> tuple[int, int]:
        return 1, 1


class _FallbackClient:
    def fetch_due(
        self, due_records: list[dict], *, now: datetime
    ) -> OddsFetchResult:
        return OddsFetchResult(
            [],
            "3",
            "497",
            used_fallback=True,
            primary_failure="HTTP 401",
        )

    def close(self) -> None:
        return None


def test_poll_outcome_propagates_fallback_usage() -> None:
    outcome = poll_odds(
        _DueStore(), now=NOW, client_factory=_FallbackClient
    )

    assert outcome.api_called
    assert outcome.used_fallback is True


class _FailingResponse:
    status_code = 503


class _FailingHTTP:
    def get(self, *args: object, **kwargs: object) -> _FailingResponse:
        return _FailingResponse()


class _ScheduleStore:
    upsert_called = False

    def upsert_schedule(self, games: list, *, now: datetime) -> tuple[int, int]:
        self.upsert_called = True
        return 0, 0


def test_espn_failure_does_not_write_or_delete_schedule() -> None:
    store = _ScheduleStore()

    with pytest.raises(RuntimeError, match="HTTP 503"):
        sync_schedule(store, now=NOW, http_client=_FailingHTTP())

    assert not store.upsert_called


class _GamesStore:
    def __init__(self, games: list[dict]) -> None:
        self.games = games
        self.upserted: list = []

    def read_games(self) -> list[dict]:
        return self.games

    def upsert_schedule(self, games: list, *, now: datetime) -> tuple[int, int]:
        self.upserted.extend(games)
        return 0, len(games)


class _RecordingResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _RecordingHTTP:
    def __init__(self, payload_by_date: dict[str, dict]) -> None:
        self.payload_by_date = payload_by_date
        self.requested_dates: list[str] = []

    def get(self, url: str, *, params: dict, headers: dict) -> _RecordingResponse:
        date = params["dates"]
        self.requested_dates.append(date)
        return _RecordingResponse(self.payload_by_date.get(date, {"events": []}))


def _final_event(event_id: str, away: int, home: int) -> dict:
    return {
        "id": event_id,
        "date": "2026-08-04T23:00:00Z",
        "status": {
            "type": {"name": "STATUS_FINAL", "state": "post", "completed": True}
        },
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"displayName": "Atlanta Dream"},
                        "score": str(home),
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": "Phoenix Mercury"},
                        "score": str(away),
                    },
                ]
            }
        ],
    }


def test_backfill_skips_http_entirely_when_nothing_is_missing() -> None:
    store = _GamesStore(
        [
            {
                "espn_event_id": "401",
                "commence_time_utc": "2026-08-04T23:00:00Z",
                "away_score": "82",
                "home_score": "96",
            },
            {
                # Not started yet -- no score expected, must not be queried.
                "espn_event_id": "402",
                "commence_time_utc": "2026-08-06T23:00:00Z",
                "away_score": "",
                "home_score": "",
            },
        ]
    )

    def factory_raises(*args: object, **kwargs: object) -> object:
        raise AssertionError("HTTP must not be called")

    created, updated = backfill_scores(
        store, now=NOW, http_client=_RecordingHTTP({})
    )

    assert (created, updated) == (0, 0)
    assert store.upserted == []


def test_backfill_fetches_the_missing_games_date_and_matches_by_espn_id() -> None:
    store = _GamesStore(
        [
            {
                "espn_event_id": "401",
                "commence_time_utc": "2026-08-04T23:00:00Z",
                "away_score": "",
                "home_score": "",
            }
        ]
    )
    http = _RecordingHTTP(
        {"20260804": {"events": [_final_event("401", 82, 96)]}}
    )

    created, updated = backfill_scores(store, now=NOW, http_client=http)

    assert http.requested_dates == ["20260804"]
    assert updated == 1
    assert store.upserted[0].espn_event_id == "401"
    assert store.upserted[0].away_score == 82
    assert store.upserted[0].home_score == 96


def test_backfill_ignores_unmatched_and_non_final_games_in_response() -> None:
    store = _GamesStore(
        [
            {
                "espn_event_id": "401",
                "commence_time_utc": "2026-08-04T23:00:00Z",
                "away_score": "",
                "home_score": "",
            }
        ]
    )
    other_event = _final_event("999", 10, 20)
    in_progress_event = _final_event("401", 5, 5)
    in_progress_event["status"]["type"] = {
        "name": "STATUS_IN_PROGRESS",
        "state": "in",
        "completed": False,
    }
    http = _RecordingHTTP(
        {"20260804": {"events": [other_event, in_progress_event]}}
    )

    created, updated = backfill_scores(store, now=NOW, http_client=http)

    assert (created, updated) == (0, 0)
    assert store.upserted == []


def test_backfill_queries_each_distinct_date_only_once() -> None:
    store = _GamesStore(
        [
            {
                "espn_event_id": "401",
                "commence_time_utc": "2026-08-04T23:00:00Z",
                "away_score": "",
                "home_score": "",
            },
            {
                "espn_event_id": "402",
                "commence_time_utc": "2026-08-03T23:00:00Z",
                "away_score": "",
                "home_score": "",
            },
        ]
    )
    http = _RecordingHTTP(
        {
            "20260804": {"events": [_final_event("401", 82, 96)]},
            "20260803": {"events": [_final_event("402", 70, 75)]},
        }
    )

    created, updated = backfill_scores(store, now=NOW, http_client=http)

    assert sorted(http.requested_dates) == ["20260803", "20260804"]
    assert updated == 2


class _ResultsStore:
    def __init__(self) -> None:
        self.upserted: list[dict] = []

    def upsert_results(self, records: list[dict]) -> tuple[int, int]:
        self.upserted.extend(records)
        return len(records), 0


def test_backfill_results_sweeps_each_date_and_records_winner() -> None:
    from wnba_poller.service import backfill_results

    store = _ResultsStore()
    http = _RecordingHTTP(
        {
            "20260508": {"events": [_final_event("401", 82, 96)]},
            "20260509": {"events": [_final_event("402", 100, 90)]},
        }
    )
    now = datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc)  # ET date 2026-05-09

    added, updated = backfill_results(
        store, start_date="20260508", now=now, http_client=http
    )

    assert http.requested_dates == ["20260508", "20260509"]
    assert added == 2
    first = store.upserted[0]
    assert first["espn_event_id"] == "401"
    assert first["game_date_et"] == "2026-05-08"
    assert first["winner"] == "Atlanta Dream"  # home 96 > away 82
    assert store.upserted[1]["winner"] == "Phoenix Mercury"  # away 100 > 90


def _allstar_event(event_id: str) -> dict:
    event = _final_event(event_id, 111, 120)
    comp = event["competitions"][0]["competitors"]
    comp[0]["team"]["displayName"] = "Team Coop"
    comp[1]["team"]["displayName"] = "Team Spoon"
    return event


def test_backfill_results_drops_exhibition_games() -> None:
    from wnba_poller.service import backfill_results

    store = _ResultsStore()
    http = _RecordingHTTP(
        {
            "20260508": {
                "events": [
                    _final_event("401", 82, 96),  # real franchises
                    _allstar_event("999"),  # all-star exhibition
                ]
            }
        }
    )
    now = datetime(2026, 5, 8, 23, 0, tzinfo=timezone.utc)

    added, _ = backfill_results(
        store, start_date="20260508", now=now, http_client=http
    )

    assert added == 1
    assert [r["espn_event_id"] for r in store.upserted] == ["401"]


def test_backfill_results_rejects_a_bad_start_date() -> None:
    from wnba_poller.service import backfill_results

    with pytest.raises(ValueError, match="YYYYMMDD"):
        backfill_results(
            _ResultsStore(), start_date="2026-05-08", now=NOW, http_client=None
        )


class _SeasonStreakStore:
    def __init__(self) -> None:
        self.upserted: list[dict] = []

    def upsert_season_streaks(self, records: list[dict]) -> tuple[int, int]:
        self.upserted.extend(records)
        return len(records), 0


def test_backfill_season_streaks_computes_and_stores_one_row_per_team() -> None:
    from wnba_poller.service import backfill_season_streaks

    # Two Dream wins over the Mercury in the 2024 window.
    payload = {"events": [_final_event("g1", 70, 90)]}  # away Mercury, home Dream
    http = _RecordingHTTP({})
    http.payload_by_date = {"20240515": payload, "20240517": payload}
    store = _SeasonStreakStore()
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)

    added, _ = backfill_season_streaks(
        store, season=2024, now=now, http_client=http
    )

    assert added == 2  # one row each for Dream and Mercury
    by_team = {r["team"]: r for r in store.upserted}
    assert by_team["Atlanta Dream"]["longest_win_streak"] == 2
    assert by_team["Atlanta Dream"]["longest_loss_streak"] == 0
    assert by_team["Phoenix Mercury"]["longest_loss_streak"] == 2
    assert by_team["Atlanta Dream"]["season"] == 2024
    assert by_team["Atlanta Dream"]["games"] == 2
