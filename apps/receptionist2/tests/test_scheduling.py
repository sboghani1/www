from datetime import UTC, datetime

from receptionist2.scheduling import next_run


def test_schedule_uses_seattle_timezone() -> None:
    result = next_run(
        "0 8 * * *",
        "America/Los_Angeles",
        after=datetime(2026, 9, 26, 18, 0, tzinfo=UTC),
    )

    assert result == datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
