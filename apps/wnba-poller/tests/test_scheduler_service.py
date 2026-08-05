from datetime import datetime, timedelta, timezone

import pytest

from wnba_poller.scheduler import is_due, poll_interval
from wnba_poller.service import poll_odds, sync_schedule

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
