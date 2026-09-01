from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from receptionist.nfl_history import (
    HISTORY_COLUMNS,
    ensure_cache,
    execute_query,
    normalize_row,
    read_metadata,
    validate_rows,
    write_cache,
)


def _row(
    event_id: str = "event-1",
    *,
    home_score: int = 24,
    away_score: int = 17,
    matchup_type: str = "division",
) -> dict:
    same_conference = matchup_type != "non_conference"
    same_division = matchup_type == "division"
    return {
        "event_id": event_id,
        "season": 2025,
        "season_type": "regular",
        "week": 1,
        "status": "final",
        "kickoff_utc": "2025-09-01T17:00:00+00:00",
        "kickoff_et": "2025-09-01T13:00:00-04:00",
        "away_team": "Away",
        "home_team": "Home",
        "away_score": away_score,
        "home_score": home_score,
        "home_result": "W",
        "home_margin": home_score - away_score,
        "total_points": home_score + away_score,
        "away_conference": "AFC",
        "away_division": "AFC East",
        "home_conference": "AFC",
        "home_division": "AFC East",
        "same_conference": "TRUE" if same_conference else "FALSE",
        "same_division": "TRUE" if same_division else "FALSE",
        "matchup_type": matchup_type,
        "division_meeting_number": 1 if same_division else "",
        "neutral_site": "FALSE",
        "overtime": False,
        "tags": "divisional_game_1,week_1",
        "source": "espn",
    }


def test_normalize_row_converts_sheet_boolean_strings() -> None:
    row = normalize_row(_row())

    assert row["same_conference"] == 1
    assert row["same_division"] == 1
    assert row["neutral_site"] == 0
    assert row["overtime"] == 0


def test_normalize_row_rejects_missing_schema_column() -> None:
    row = _row()
    del row["event_id"]

    with pytest.raises(ValueError, match="missing columns"):
        normalize_row(row)


def test_validate_rows_checks_score_arithmetic() -> None:
    row = _row()
    row["home_margin"] = 99

    with pytest.raises(ValueError, match="inconsistent score fields"):
        validate_rows(
            [row],
            expected_rows=1,
            expected_seasons={2025},
            expected_matchups={"division": 1},
        )


def test_validate_rows_ignores_future_season_rows() -> None:
    current = _row()
    future = _row("future")
    future["season"] = 2026
    future["status"] = "scheduled"
    future["same_conference"] = ""
    future["same_division"] = ""
    future["neutral_site"] = ""
    future["overtime"] = ""

    rows = validate_rows(
        [current, future],
        expected_rows=1,
        expected_seasons={2025},
        expected_matchups={"division": 1},
    )

    assert [row["event_id"] for row in rows] == ["event-1"]


def test_validate_rows_checks_matchup_consistency() -> None:
    row = _row()
    row["same_division"] = "FALSE"

    with pytest.raises(ValueError, match="inconsistent matchup fields"):
        validate_rows(
            [row],
            expected_rows=1,
            expected_seasons={2025},
            expected_matchups={"division": 1},
        )


def test_cache_query_and_metadata(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache([_row()], cache, validate=False)

    result = execute_query(
        """
        SELECT season, COUNT(*) AS games,
               SUM(CASE WHEN home_result = 'W' THEN 1 ELSE 0 END) AS wins
        FROM games
        GROUP BY season
        """,
        cache,
    )

    assert result["columns"] == ["season", "games", "wins"]
    assert result["rows"] == [{"season": 2025, "games": 1, "wins": 1}]
    assert result["cache"]["row_count"] == "1"
    assert read_metadata(cache)["source_tab"] == "nfl_game_history"


def test_query_accepts_common_table_expression(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache([_row()], cache, validate=False)

    result = execute_query(
        "WITH filtered AS (SELECT * FROM games) "
        "SELECT COUNT(*) AS games FROM filtered",
        cache,
    )

    assert result["rows"] == [{"games": 1}]


def test_query_rejects_mutation_and_multiple_statements(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache([_row()], cache, validate=False)

    with pytest.raises(ValueError, match="Only SELECT"):
        execute_query("DELETE FROM games", cache)
    with pytest.raises(ValueError, match="one SQL statement"):
        execute_query("SELECT 1; SELECT 2", cache)

    connection = sqlite3.connect(cache)
    try:
        assert connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    finally:
        connection.close()


def test_query_aborts_runaway_recursive_cte(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache([_row()], cache, validate=False)

    with pytest.raises(ValueError, match="execution limit"):
        execute_query(
            "WITH RECURSIVE forever(n) AS "
            "(SELECT 1 UNION ALL SELECT n + 1 FROM forever) "
            "SELECT COUNT(*) FROM forever",
            cache,
        )


def test_query_requires_bounded_result(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    rows = [_row(str(index)) for index in range(3)]
    write_cache(rows, cache, validate=False)

    with pytest.raises(ValueError, match="more than 2 rows"):
        execute_query("SELECT * FROM games", cache, max_rows=2)


def test_ensure_cache_reuses_valid_version_without_loader(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache(
        [_row(f"event-{index:04d}") for index in range(816)],
        cache,
        validate=False,
    )

    def fail_loader() -> list[dict]:
        raise AssertionError("valid cache should not load the Sheet")

    result = ensure_cache(cache, loader=fail_loader)

    assert result["row_count"] == "816"
    assert set(HISTORY_COLUMNS).issuperset({"season", "home_result"})


def test_ensure_cache_reloads_corrupt_cache(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache(
        [_row(f"event-{index:04d}") for index in range(816)],
        cache,
        validate=False,
    )
    connection = sqlite3.connect(cache)
    try:
        connection.execute(
            "UPDATE games SET home_score = 99 WHERE event_id = 'event-0000'"
        )
        connection.commit()
    finally:
        connection.close()
    loaded = False

    def loader() -> list[dict]:
        nonlocal loaded
        loaded = True
        return [_row()]

    with pytest.raises(ValueError, match="has 1 rows"):
        ensure_cache(cache, loader=loader)

    assert loaded


def test_failed_refresh_preserves_existing_cache(tmp_path: Path) -> None:
    cache = tmp_path / "history.sqlite3"
    write_cache(
        [_row(f"event-{index:04d}") for index in range(816)],
        cache,
        validate=False,
    )
    original_hash = read_metadata(cache)["content_sha256"]

    def fail_loader() -> list[dict]:
        raise RuntimeError("Sheet unavailable")

    with pytest.raises(RuntimeError, match="Sheet unavailable"):
        ensure_cache(cache, refresh=True, loader=fail_loader)

    assert read_metadata(cache)["content_sha256"] == original_hash
