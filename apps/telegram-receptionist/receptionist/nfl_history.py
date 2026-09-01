from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

CACHE_SCHEMA_VERSION = "1"
DATASET_ID = "nfl-game-history-2023-2025-v1"
DEFAULT_CACHE_PATH = (
    Path.home() / ".cache" / "nfl-game-history" / "history.sqlite3"
)
HISTORY_TAB = "nfl_game_history"
EXPECTED_ROWS = 816
EXPECTED_SEASONS = {2023, 2024, 2025}
EXPECTED_MATCHUPS_PER_SEASON = {
    "division": 96,
    "conference": 96,
    "non_conference": 80,
}
MAX_QUERY_ROWS = 500
QUERY_PROGRESS_STEPS = 1_000
QUERY_STEP_LIMIT = 1_000_000

HISTORY_COLUMNS = [
    "event_id",
    "season",
    "season_type",
    "week",
    "status",
    "kickoff_utc",
    "kickoff_et",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
    "home_result",
    "home_margin",
    "total_points",
    "away_conference",
    "away_division",
    "home_conference",
    "home_division",
    "same_conference",
    "same_division",
    "matchup_type",
    "division_meeting_number",
    "neutral_site",
    "overtime",
    "tags",
    "source",
]

INTEGER_COLUMNS = {
    "season",
    "week",
    "away_score",
    "home_score",
    "home_margin",
    "total_points",
    "division_meeting_number",
    "same_conference",
    "same_division",
    "neutral_site",
    "overtime",
}
BOOLEAN_COLUMNS = {
    "same_conference",
    "same_division",
    "neutral_site",
    "overtime",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    missing = set(HISTORY_COLUMNS) - set(row)
    if missing:
        raise ValueError(
            f"NFL history row is missing columns: {sorted(missing)}"
        )
    normalized: dict[str, Any] = {}
    for column in HISTORY_COLUMNS:
        value = row[column]
        if column in BOOLEAN_COLUMNS:
            normalized[column] = int(_as_bool(value))
        elif column in INTEGER_COLUMNS:
            normalized[column] = (
                None if value in ("", None) else int(value)
            )
        else:
            normalized[column] = str(value)
    return normalized


def validate_rows(
    rows: list[dict[str, Any]],
    *,
    expected_rows: int = EXPECTED_ROWS,
    expected_seasons: set[int] = EXPECTED_SEASONS,
    expected_matchups: dict[str, int] = EXPECTED_MATCHUPS_PER_SEASON,
) -> list[dict[str, Any]]:
    target_rows = [
        row
        for row in rows
        if str(row.get("season") or "").isdigit()
        and int(row["season"]) in expected_seasons
        and str(row.get("season_type") or "") == "regular"
        and str(row.get("status") or "") == "final"
    ]
    normalized = [normalize_row(row) for row in target_rows]
    if len(normalized) != expected_rows:
        raise ValueError(
            f"{HISTORY_TAB} has {len(normalized)} rows, "
            f"expected {expected_rows}"
        )
    event_ids = [row["event_id"] for row in normalized]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError(f"{HISTORY_TAB} contains duplicate event IDs")
    seasons = {int(row["season"]) for row in normalized}
    if seasons != expected_seasons:
        raise ValueError(
            f"{HISTORY_TAB} seasons {seasons} do not match "
            f"{expected_seasons}"
        )
    for season in sorted(expected_seasons):
        season_rows = [
            row for row in normalized if int(row["season"]) == season
        ]
        if len(season_rows) != expected_rows // len(expected_seasons):
            raise ValueError(f"{season} has an unexpected game count")
        matchups = Counter(row["matchup_type"] for row in season_rows)
        if dict(matchups) != expected_matchups:
            raise ValueError(
                f"{season} matchup counts {dict(matchups)} do not match "
                f"{expected_matchups}"
            )
    for row in normalized:
        margin = int(row["home_score"]) - int(row["away_score"])
        result = "W" if margin > 0 else "L" if margin < 0 else "T"
        if (
            int(row["home_margin"]) != margin
            or int(row["total_points"])
            != int(row["home_score"]) + int(row["away_score"])
            or row["home_result"] != result
        ):
            raise ValueError(
                f"Event {row['event_id']} has inconsistent score fields"
            )
        matchup_type = row["matchup_type"]
        same_conference = bool(row["same_conference"])
        same_division = bool(row["same_division"])
        meeting = row["division_meeting_number"]
        valid_classification = (
            matchup_type == "division"
            and same_conference
            and same_division
            and meeting in (1, 2)
        ) or (
            matchup_type == "conference"
            and same_conference
            and not same_division
            and meeting is None
        ) or (
            matchup_type == "non_conference"
            and not same_conference
            and not same_division
            and meeting is None
        )
        if not valid_classification:
            raise ValueError(
                f"Event {row['event_id']} has inconsistent matchup fields"
            )
    return normalized


def fetch_sheet_rows() -> list[dict[str, Any]]:
    import gspread
    from google.auth.exceptions import GoogleAuthError
    from google.oauth2.service_account import Credentials
    from requests.exceptions import RequestException

    credentials_b64 = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    credentials_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = os.getenv("NFL_INTAKE_SHEET_ID", "").strip()
    if not sheet_id:
        raise ValueError("NFL_INTAKE_SHEET_ID is required")
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    try:
        if credentials_b64:
            info = json.loads(
                base64.b64decode(credentials_b64).decode("utf-8")
            )
            credentials = Credentials.from_service_account_info(
                info, scopes=scopes
            )
        elif credentials_path:
            credentials = Credentials.from_service_account_file(
                credentials_path, scopes=scopes
            )
        else:
            raise ValueError(
                "Google service-account credentials are required"
            )
        worksheet = (
            gspread.authorize(credentials)
            .open_by_key(sheet_id)
            .worksheet(HISTORY_TAB)
        )
        values = worksheet.get_all_values()
    except (GoogleAuthError, RequestException) as exc:
        raise RuntimeError(
            f"Could not authenticate or connect to {HISTORY_TAB}"
        ) from exc
    except gspread.exceptions.GSpreadException as exc:
        raise RuntimeError(
            f"Could not read the {HISTORY_TAB} worksheet"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "GOOGLE_CREDENTIALS is not valid base64 JSON"
        ) from exc
    if not values:
        raise ValueError(f"{HISTORY_TAB} is empty")
    headers = values[0]
    if headers != HISTORY_COLUMNS:
        raise ValueError(
            f"{HISTORY_TAB} headers do not match the expected schema"
        )
    rows = []
    for values_row in values[1:]:
        if not any(values_row):
            continue
        padded = [*values_row, *([""] * (len(headers) - len(values_row)))]
        rows.append(dict(zip(headers, padded, strict=False)))
    return rows


def _content_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_cache(
    rows: list[dict[str, Any]],
    cache_path: Path = DEFAULT_CACHE_PATH,
    *,
    validate: bool = True,
) -> dict[str, str]:
    normalized = validate_rows(rows) if validate else [
        normalize_row(row) for row in rows
    ]
    normalized.sort(key=lambda row: row["event_id"])
    cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fetched_at = datetime.now(UTC).isoformat()
    metadata = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "source_tab": HISTORY_TAB,
        "row_count": str(len(normalized)),
        "fetched_at": fetched_at,
        "content_sha256": _content_hash(normalized),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{cache_path.name}.",
        suffix=".tmp",
        dir=cache_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            definitions = ", ".join(
                f'"{column}" '
                + ("INTEGER" if column in INTEGER_COLUMNS else "TEXT")
                for column in HISTORY_COLUMNS
            )
            connection.execute(f"CREATE TABLE games ({definitions})")
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)"
            )
            placeholders = ",".join("?" for _ in HISTORY_COLUMNS)
            connection.executemany(
                f"INSERT INTO games VALUES ({placeholders})",
                [
                    tuple(row[column] for column in HISTORY_COLUMNS)
                    for row in normalized
                ],
            )
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                metadata.items(),
            )
            connection.execute(
                "CREATE INDEX games_season_week "
                "ON games (season, week)"
            )
            connection.execute(
                "CREATE INDEX games_matchup "
                "ON games (matchup_type, division_meeting_number)"
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, cache_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return metadata


def read_metadata(cache_path: Path = DEFAULT_CACHE_PATH) -> dict[str, str]:
    connection = sqlite3.connect(
        f"file:{cache_path}?mode=ro",
        uri=True,
    )
    try:
        return dict(connection.execute("SELECT key, value FROM metadata"))
    finally:
        connection.close()


def cache_is_valid(cache_path: Path = DEFAULT_CACHE_PATH) -> bool:
    try:
        connection = sqlite3.connect(
            f"file:{cache_path}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            metadata = dict(
                connection.execute("SELECT key, value FROM metadata")
            )
            columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(games)")
            ]
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM games ORDER BY event_id"
                )
            ]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False
    return (
        metadata.get("cache_schema_version") == CACHE_SCHEMA_VERSION
        and metadata.get("dataset_id") == DATASET_ID
        and metadata.get("row_count") == str(EXPECTED_ROWS)
        and len(rows) == EXPECTED_ROWS
        and columns == HISTORY_COLUMNS
        and metadata.get("content_sha256") == _content_hash(rows)
    )


def ensure_cache(
    cache_path: Path = DEFAULT_CACHE_PATH,
    *,
    refresh: bool = False,
    loader: Callable[[], list[dict[str, Any]]] = fetch_sheet_rows,
) -> dict[str, str]:
    if not refresh and cache_path.is_file() and cache_is_valid(cache_path):
        return read_metadata(cache_path)
    return write_cache(loader(), cache_path)


def _readonly_sql(sql: str) -> str:
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement:
        raise ValueError("Only one SQL statement is allowed")
    first_word = statement.split(None, 1)[0].lower() if statement else ""
    if first_word not in {"select", "with"}:
        raise ValueError("Only SELECT or WITH queries are allowed")
    return statement


def execute_query(
    sql: str,
    cache_path: Path = DEFAULT_CACHE_PATH,
    *,
    max_rows: int = MAX_QUERY_ROWS,
) -> dict[str, Any]:
    statement = _readonly_sql(sql)
    connection = sqlite3.connect(
        f"file:{cache_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        steps = 0

        def enforce_step_limit() -> int:
            nonlocal steps
            steps += QUERY_PROGRESS_STEPS
            return int(steps > QUERY_STEP_LIMIT)

        connection.set_progress_handler(
            enforce_step_limit,
            QUERY_PROGRESS_STEPS,
        )
        try:
            cursor = connection.execute(statement)
            result = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted":
                raise ValueError(
                    "Query exceeded the execution limit; simplify it"
                ) from exc
            raise
        if len(result) > max_rows:
            raise ValueError(
                f"Query returned more than {max_rows} rows; aggregate or "
                "narrow it"
            )
        return {
            "columns": [
                description[0] for description in cursor.description or []
            ],
            "rows": [dict(row) for row in result],
            "row_count": len(result),
            "cache": read_metadata(cache_path),
        }
    finally:
        connection.close()


def schema_payload(
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> dict[str, Any]:
    return {
        "table": "games",
        "columns": {
            column: (
                "INTEGER" if column in INTEGER_COLUMNS else "TEXT"
            )
            for column in HISTORY_COLUMNS
        },
        "boolean_columns": sorted(BOOLEAN_COLUMNS),
        "metadata": read_metadata(cache_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only cached NFL game-history queries."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("schema")
    subparsers.add_parser("refresh")
    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--sql", required=True)
    args = parser.parse_args()

    try:
        if args.command == "refresh":
            output: dict[str, Any] = {
                "ok": True,
                "cache": ensure_cache(
                    DEFAULT_CACHE_PATH,
                    refresh=True,
                ),
            }
        else:
            ensure_cache(DEFAULT_CACHE_PATH)
            output = (
                schema_payload(DEFAULT_CACHE_PATH)
                if args.command == "schema"
                else execute_query(args.sql, DEFAULT_CACHE_PATH)
            )
        print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
