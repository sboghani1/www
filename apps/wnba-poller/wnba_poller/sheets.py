from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import gspread
from google.oauth2.service_account import Credentials

from .models import (
    ET,
    LINE_FIELDS,
    NO_DATA,
    OddsLines,
    ScheduleGame,
    eastern_timestamp,
    parse_timestamp,
    utc_timestamp,
)
from .lean_revisions import (
    LEAN_REVISION_HEADERS,
    derive_revision_history,
)
from .scheduler import due_games, next_poll_timestamp

RAW_VALUE_INPUT_OPTION = "RAW"
EXPECTED_WORKBOOK_TITLE = "asce Guesser"
SHEET_TABS = {
    "games": "wnba_games",
    "snapshots": "wnba_line_snapshots",
    "thoughts": "wnba_thoughts",
    "lean_revisions": "wnba_lean_revisions",
    "allowed_users": "wnba_allowed_users",
    "settings": "wnba_settings",
}
LEGACY_NFL_TABS = ("nfl_games", "nfl_line_snapshots", "nfl_leans")

GAME_HEADERS = [
    "event_id",
    "espn_event_id",
    "status",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "venue",
    "broadcast",
    "bookmaker",
    "opening_captured_at",
    "latest_captured_at",
    *(f"opening_{field}" for field in LINE_FIELDS),
    *(f"latest_{field}" for field in LINE_FIELDS),
    "last_updated_at",
    "last_odds_polled_at",
    "next_poll_at",
    "manual_status",
    "user_notes",
    "user_tags",
]
GAME_MACHINE_HEADERS = GAME_HEADERS[: GAME_HEADERS.index("manual_status")]

SNAPSHOT_HEADERS = [
    "captured_at_utc",
    "captured_at_et",
    "event_id",
    "espn_event_id",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    *LINE_FIELDS,
    "api_requests_used",
    "api_requests_remaining",
]

THOUGHT_HEADERS = [
    "thought_id",
    "submitted_at_utc",
    "submitted_at_et",
    "source",
    "telegram_user_id",
    "telegram_username",
    "telegram_chat_id",
    "telegram_message_id",
    "event_id",
    "espn_event_id",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    "period",
    "market",
    "side",
    "opening_selected_line",
    "opening_selected_price",
    "latest_selected_line",
    "latest_selected_price",
    "latest_captured_at",
    *(f"frozen_{field}" for field in LINE_FIELDS),
    "thought_text",
    "processed_into_path210_at",
]
THOUGHT_FROZEN_HEADERS = THOUGHT_HEADERS[
    : THOUGHT_HEADERS.index("thought_text")
]

ALLOWED_USER_HEADERS = [
    "telegram_user_id",
    "telegram_username",
    "display_name",
    "enabled",
    "added_at_utc",
    "notes",
]

SETTINGS_HEADERS = ["key", "value", "updated_at_utc", "description"]

TAB_HEADERS = {
    SHEET_TABS["games"]: GAME_HEADERS,
    SHEET_TABS["snapshots"]: SNAPSHOT_HEADERS,
    SHEET_TABS["thoughts"]: THOUGHT_HEADERS,
    SHEET_TABS["lean_revisions"]: LEAN_REVISION_HEADERS,
    SHEET_TABS["allowed_users"]: ALLOWED_USER_HEADERS,
    SHEET_TABS["settings"]: SETTINGS_HEADERS,
}

SETTING_DESCRIPTIONS = {
    "schema_version": "WNBA workbook schema version",
    "schedule_horizon_days": "Rolling ESPN schedule horizon",
    "timezone": "Display timezone",
    "poll_far_minutes": "Poll interval more than six hours before tip",
    "poll_near_minutes": "Poll interval within six hours of tip",
    "poll_near_threshold_hours": "Threshold for faster polling",
    "last_successful_schedule_sync": "Last completed ESPN schedule upsert",
    "last_successful_odds_poll": "Last completed Odds API poll",
    "last_api_requests_used": "Most recent Odds API usage header",
    "last_api_requests_remaining": "Most recent Odds API remaining header",
    "service_version": "Installed package version",
}

DEFAULT_SETTINGS = {
    "schema_version": "3",
    "schedule_horizon_days": "14",
    "timezone": "America/New_York",
    "poll_far_minutes": "60",
    "poll_near_minutes": "15",
    "poll_near_threshold_hours": "6",
    "last_successful_schedule_sync": "",
    "last_successful_odds_poll": "",
    "last_api_requests_used": "",
    "last_api_requests_remaining": "",
    "service_version": "0.3.0",
}


def normalize_sheet_row(
    headers: list[str],
    row: Iterable[Any],
) -> dict[str, Any]:
    values = list(row)
    values.extend("" for _ in range(max(0, len(headers) - len(values))))
    return dict(zip(headers, values[: len(headers)]))


def row_values(record: dict[str, Any], headers: list[str]) -> list[Any]:
    return [
        "" if record.get(header) is None else record.get(header, "")
        for header in headers
    ]


def require_headers(
    values: list[list[Any]],
    expected: list[str],
    tab_name: str,
) -> None:
    actual = list(values[0]) if values else []
    if actual != expected:
        raise ValueError(
            f"{tab_name} headers do not match the expected schema"
        )


def _empty_game_record() -> dict[str, Any]:
    return {header: "" for header in GAME_HEADERS}


def merge_schedule_record(
    existing: dict[str, Any] | None,
    game: ScheduleGame,
    *,
    now: datetime,
) -> dict[str, Any]:
    record = _empty_game_record()
    if existing:
        record.update(existing)
    record.update(
        {
            "espn_event_id": game.espn_event_id,
            "status": game.status,
            "commence_time_utc": game.commence_time_utc,
            "commence_time_et": game.commence_time_et,
            "away_team": game.away_team,
            "home_team": game.home_team,
            "venue": game.venue,
            "broadcast": game.broadcast,
            "last_updated_at": utc_timestamp(now),
        }
    )
    if not record.get("last_odds_polled_at"):
        record["next_poll_at"] = utc_timestamp(now)
    return {header: record.get(header, "") for header in GAME_HEADERS}


def line_cell(value: Any) -> Any:
    return NO_DATA if value is None else value


def parse_line_cell(value: Any) -> float | int | None:
    if value in ("", None, NO_DATA):
        return None
    if isinstance(value, (float, int)):
        return value
    number = float(str(value))
    return int(number) if number.is_integer() else number


def apply_odds_lines(
    existing: dict[str, Any],
    lines: OddsLines,
    *,
    now: datetime,
) -> dict[str, Any]:
    record = _empty_game_record()
    record.update(existing)
    record.update(
        {
            "event_id": lines.event_id,
            "espn_event_id": (
                lines.espn_event_id or record.get("espn_event_id", "")
            ),
            "status": "scheduled",
            "commence_time_utc": lines.commence_time_utc,
            "commence_time_et": lines.commence_time_et,
            "away_team": lines.away_team,
            "home_team": lines.home_team,
            "bookmaker": lines.bookmaker,
            "latest_captured_at": lines.captured_at_utc,
            "last_updated_at": utc_timestamp(now),
            "last_odds_polled_at": utc_timestamp(now),
            "next_poll_at": next_poll_timestamp(
                lines.commence_time_utc, now
            ),
        }
    )
    has_line = any(getattr(lines, field) is not None for field in LINE_FIELDS)
    if has_line and not record.get("opening_captured_at"):
        record["opening_captured_at"] = lines.captured_at_utc
    for field in LINE_FIELDS:
        candidate = getattr(lines, field)
        opening_key = f"opening_{field}"
        latest_key = f"latest_{field}"
        if record.get(opening_key) in ("", None, NO_DATA):
            record[opening_key] = line_cell(candidate)
        record[latest_key] = line_cell(candidate)
    return {header: record.get(header, "") for header in GAME_HEADERS}


def mark_odds_poll(
    existing: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    record = _empty_game_record()
    record.update(existing)
    record["last_odds_polled_at"] = utc_timestamp(now)
    record["last_updated_at"] = utc_timestamp(now)
    commence = str(record.get("commence_time_utc") or "")
    record["next_poll_at"] = (
        next_poll_timestamp(commence, now) if commence else ""
    )
    return {header: record.get(header, "") for header in GAME_HEADERS}


def snapshot_record(lines: OddsLines) -> dict[str, Any]:
    record = {
        "captured_at_utc": lines.captured_at_utc,
        "captured_at_et": eastern_timestamp(
            parse_timestamp(lines.captured_at_utc)
        ),
        "event_id": lines.event_id,
        "espn_event_id": lines.espn_event_id,
        "commence_time_utc": lines.commence_time_utc,
        "commence_time_et": lines.commence_time_et,
        "away_team": lines.away_team,
        "home_team": lines.home_team,
        "bookmaker": lines.bookmaker,
        "api_requests_used": lines.api_requests_used,
        "api_requests_remaining": lines.api_requests_remaining,
    }
    for field in LINE_FIELDS:
        record[field] = line_cell(getattr(lines, field))
    return {header: record.get(header, "") for header in SNAPSHOT_HEADERS}


def snapshot_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(parse_line_cell(record.get(field)) for field in LINE_FIELDS)


def should_append_snapshot(
    lines: OddsLines,
    previous: dict[str, Any] | None,
) -> bool:
    if previous is None:
        return True
    if lines.signature() != snapshot_signature(previous):
        return True
    captured = str(previous.get("captured_at_utc") or "")
    if not captured:
        return True
    return (
        parse_timestamp(lines.captured_at_utc) - parse_timestamp(captured)
        >= timedelta(minutes=60)
    )


def _normalized_team(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _find_game_for_thought(
    thought: dict[str, Any],
    games: list[dict[str, Any]],
) -> dict[str, Any] | None:
    event_id = str(thought.get("event_id") or "")
    espn_event_id = str(thought.get("espn_event_id") or "")
    if event_id:
        match = next(
            (
                game
                for game in games
                if event_id
                in {
                    str(game.get("event_id") or ""),
                    str(game.get("espn_event_id") or ""),
                }
            ),
            None,
        )
        if match:
            return match
    if espn_event_id:
        match = next(
            (
                game
                for game in games
                if str(game.get("espn_event_id") or "") == espn_event_id
            ),
            None,
        )
        if match:
            return match

    away = _normalized_team(thought.get("away_team"))
    home = _normalized_team(thought.get("home_team"))
    if not away or not home:
        return None
    matches = [
        game
        for game in games
        if _normalized_team(game.get("away_team")) == away
        and _normalized_team(game.get("home_team")) == home
    ]
    return matches[0] if len(matches) == 1 else None


def plan_thought_reconciliation(
    indexed_thoughts: list[tuple[int, dict[str, Any]]],
    games: list[dict[str, Any]],
    *,
    now: datetime,
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    updates: list[tuple[int, dict[str, Any]]] = []
    unresolved = 0
    for row_number, original in indexed_thoughts:
        thought = normalize_sheet_row(
            THOUGHT_HEADERS,
            row_values(original, THOUGHT_HEADERS),
        )
        if not str(thought.get("thought_text") or ""):
            continue
        if thought.get("thought_id"):
            continue
        game = _find_game_for_thought(thought, games)
        if game is None:
            unresolved += 1
            continue

        exact_text = thought["thought_text"]
        thought.update(
            {
                "thought_id": id_factory(),
                "submitted_at_utc": utc_timestamp(now),
                "submitted_at_et": eastern_timestamp(now),
                "source": "sheet",
                "event_id": game.get("event_id", ""),
                "espn_event_id": game.get("espn_event_id", ""),
                "commence_time_utc": game.get("commence_time_utc", ""),
                "commence_time_et": game.get("commence_time_et", ""),
                "away_team": game.get("away_team", ""),
                "home_team": game.get("home_team", ""),
                "bookmaker": game.get("bookmaker", ""),
                "latest_captured_at": game.get(
                    "latest_captured_at", ""
                ),
            }
        )
        for field in LINE_FIELDS:
            thought[f"frozen_{field}"] = (
                game.get(f"latest_{field}") or NO_DATA
            )
        thought["thought_text"] = exact_text
        updates.append(
            (
                row_number,
                {
                    header: thought.get(header, "")
                    for header in THOUGHT_HEADERS
                },
            )
        )
    return updates, unresolved


def build_thought_record(
    game: dict[str, Any],
    *,
    thought_id: str,
    thought_text: str,
    source: str,
    now: datetime,
    telegram_metadata: dict[str, Any] | None = None,
    selection_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not thought_id:
        raise ValueError("thought_id is required")
    if not thought_text:
        raise ValueError("thought_text is required")
    if source not in {"sheet", "telegram"}:
        raise ValueError("source must be sheet or telegram")
    metadata = telegram_metadata or {}
    selection = selection_metadata or {}
    record = {header: "" for header in THOUGHT_HEADERS}
    record.update(
        {
            "thought_id": thought_id,
            "submitted_at_utc": utc_timestamp(now),
            "submitted_at_et": eastern_timestamp(now),
            "source": source,
            "telegram_user_id": metadata.get("telegram_user_id", ""),
            "telegram_username": metadata.get("telegram_username", ""),
            "telegram_chat_id": metadata.get("telegram_chat_id", ""),
            "telegram_message_id": metadata.get("telegram_message_id", ""),
            "event_id": game.get("event_id", ""),
            "espn_event_id": game.get("espn_event_id", ""),
            "commence_time_utc": game.get("commence_time_utc", ""),
            "commence_time_et": game.get("commence_time_et", ""),
            "away_team": game.get("away_team", ""),
            "home_team": game.get("home_team", ""),
            "bookmaker": game.get("bookmaker", ""),
            "period": selection.get("period", ""),
            "market": selection.get("market", ""),
            "side": selection.get("side", ""),
            "opening_selected_line": selection.get(
                "opening_selected_line", ""
            ),
            "opening_selected_price": selection.get(
                "opening_selected_price", ""
            ),
            "latest_selected_line": selection.get(
                "latest_selected_line", ""
            ),
            "latest_selected_price": selection.get(
                "latest_selected_price", ""
            ),
            "latest_captured_at": game.get("latest_captured_at", ""),
            "thought_text": thought_text,
        }
    )
    for field in LINE_FIELDS:
        record[f"frozen_{field}"] = (
            game.get(f"latest_{field}") or NO_DATA
        )
    return record


def is_duplicate_thought(
    candidate: dict[str, Any],
    existing: Iterable[dict[str, Any]],
) -> bool:
    return find_duplicate_thought(candidate, existing) is not None


def find_duplicate_thought(
    candidate: dict[str, Any],
    existing: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    thought_id = str(candidate.get("thought_id") or "")
    chat_id = str(candidate.get("telegram_chat_id") or "")
    message_id = str(candidate.get("telegram_message_id") or "")
    for record in existing:
        if thought_id and str(record.get("thought_id") or "") == thought_id:
            return record
        if (
            candidate.get("source") == "telegram"
            and chat_id
            and message_id
            and str(record.get("telegram_chat_id") or "") == chat_id
            and str(record.get("telegram_message_id") or "") == message_id
        ):
            return record
    return None


def get_gspread_client(
    *,
    credentials_b64: str,
    service_account_json: str,
) -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if credentials_b64:
        try:
            info = json.loads(
                base64.b64decode(credentials_b64).decode("utf-8")
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("GOOGLE_CREDENTIALS is not valid base64 JSON") from exc
        credentials = Credentials.from_service_account_info(
            info, scopes=scopes
        )
    elif service_account_json:
        credentials = Credentials.from_service_account_file(
            service_account_json, scopes=scopes
        )
    else:
        raise ValueError("Google service-account credentials are required")
    return gspread.authorize(credentials)


class SheetsStore:
    def __init__(self, spreadsheet: Any) -> None:
        self.spreadsheet = spreadsheet

    @classmethod
    def connect(
        cls,
        *,
        sheet_id: str,
        credentials_b64: str,
        service_account_json: str,
    ) -> "SheetsStore":
        client = get_gspread_client(
            credentials_b64=credentials_b64,
            service_account_json=service_account_json,
        )
        spreadsheet = client.open_by_key(sheet_id)
        if spreadsheet.title != EXPECTED_WORKBOOK_TITLE:
            raise ValueError(
                "WNBA_SHEET_ID does not reference the expected workbook"
            )
        return cls(spreadsheet)

    @staticmethod
    def _call(
        operation: Callable[..., Any],
        *args: Any,
        retries: int = 5,
        **kwargs: Any,
    ) -> Any:
        delay = 2
        for attempt in range(retries):
            try:
                return operation(*args, **kwargs)
            except gspread.exceptions.APIError as exc:
                status = getattr(exc.response, "status_code", None)
                if status not in {429, 500, 502, 503, 504} or attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError("Google Sheets operation failed")

    def _worksheet(self, tab_name: str) -> Any:
        return self.spreadsheet.worksheet(tab_name)

    def _indexed_records(
        self,
        tab_name: str,
        headers: list[str],
        *,
        formulas: bool = False,
    ) -> list[tuple[int, dict[str, Any]]]:
        worksheet = self._worksheet(tab_name)
        kwargs = {"value_render_option": "FORMULA"} if formulas else {}
        values = self._call(worksheet.get_all_values, **kwargs)
        require_headers(values, headers, tab_name)
        records: list[tuple[int, dict[str, Any]]] = []
        for row_number, row in enumerate(values[1:], start=2):
            if not any(value != "" for value in row):
                continue
            records.append(
                (row_number, normalize_sheet_row(headers, row))
            )
        return records

    def read_games(self) -> list[dict[str, Any]]:
        return [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["games"], GAME_HEADERS
            )
        ]

    def backup_workbook(self, output_path: Path) -> None:
        if output_path.exists():
            raise FileExistsError(
                f"Backup already exists: {output_path}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tabs = []
        for worksheet in self._call(self.spreadsheet.worksheets):
            tabs.append(
                {
                    "title": worksheet.title,
                    "id": worksheet.id,
                    "row_count": worksheet.row_count,
                    "col_count": worksheet.col_count,
                    "values": self._call(
                        worksheet.get_all_values,
                        value_render_option="FORMULA",
                    ),
                }
            )
        output_path.write_text(
            json.dumps({"worksheets": tabs}, indent=2),
            encoding="utf-8",
        )

    def initialize(
        self,
        *,
        replace_tabs: bool,
        backup_path: Path | None,
        remove_legacy_nfl_tabs: bool,
        seed_allowed_user_id: int | None = None,
        seed_allowed_username: str = "",
        seed_allowed_display_name: str = "",
    ) -> tuple[int, int]:
        destructive = replace_tabs or remove_legacy_nfl_tabs
        if destructive and backup_path is None:
            raise ValueError(
                "A backup path is required for destructive initialization"
            )
        if backup_path is not None:
            self.backup_workbook(backup_path)

        worksheets = {
            worksheet.title: worksheet
            for worksheet in self._call(self.spreadsheet.worksheets)
        }
        removed = 0
        if remove_legacy_nfl_tabs:
            for title in LEGACY_NFL_TABS:
                worksheet = worksheets.pop(title, None)
                if worksheet is not None:
                    self._call(self.spreadsheet.del_worksheet, worksheet)
                    removed += 1

        created = 0
        for tab_name, headers in TAB_HEADERS.items():
            worksheet = worksheets.get(tab_name)
            if worksheet is not None and replace_tabs:
                self._call(self.spreadsheet.del_worksheet, worksheet)
                worksheet = None
                removed += 1
            if worksheet is None:
                worksheet = self._call(
                    self.spreadsheet.add_worksheet,
                    title=tab_name,
                    rows=2000,
                    cols=len(headers),
                )
                self._call(
                    worksheet.update,
                    range_name="A1",
                    values=[headers],
                    value_input_option=RAW_VALUE_INPUT_OPTION,
                )
                created += 1
            else:
                values = self._call(worksheet.get_all_values)
                require_headers(values, headers, tab_name)

        self.update_settings(DEFAULT_SETTINGS, now=datetime.now(tz=ET))
        if seed_allowed_user_id is not None:
            self.seed_allowed_user(
                user_id=seed_allowed_user_id,
                username=seed_allowed_username,
                display_name=seed_allowed_display_name,
                now=datetime.now(tz=ET),
            )
        return created, removed

    def read_allowed_users(self) -> list[dict[str, Any]]:
        return [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["allowed_users"], ALLOWED_USER_HEADERS
            )
        ]

    def allowed_user_ids(self) -> set[int]:
        allowed: set[int] = set()
        for record in self.read_allowed_users():
            enabled = str(record.get("enabled") or "").strip().lower()
            if enabled not in {"1", "true", "yes", "y", "enabled"}:
                continue
            try:
                user_id = int(str(record.get("telegram_user_id") or ""))
            except ValueError:
                continue
            if user_id > 0:
                allowed.add(user_id)
        return allowed

    def seed_allowed_user(
        self,
        *,
        user_id: int,
        username: str,
        display_name: str,
        now: datetime,
    ) -> bool:
        if user_id <= 0:
            raise ValueError("allowed Telegram user ID must be positive")
        existing = self.read_allowed_users()
        if any(
            str(record.get("telegram_user_id") or "") == str(user_id)
            for record in existing
        ):
            return False
        record = {
            "telegram_user_id": user_id,
            "telegram_username": username[:64],
            "display_name": display_name[:200],
            "enabled": "true",
            "added_at_utc": utc_timestamp(now),
            "notes": "Initial WNBA Guesser allowlist seed",
        }
        self._call(
            self._worksheet(SHEET_TABS["allowed_users"]).append_rows,
            [row_values(record, ALLOWED_USER_HEADERS)],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        return True

    def update_settings(
        self,
        updates: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        indexed = self._indexed_records(
            SHEET_TABS["settings"], SETTINGS_HEADERS
        )
        by_key = {
            str(record["key"]): (row_number, record)
            for row_number, record in indexed
            if record.get("key")
        }
        worksheet = self._worksheet(SHEET_TABS["settings"])
        batch: list[dict[str, Any]] = []
        new_rows: list[list[Any]] = []
        for key, value in updates.items():
            record = {
                "key": key,
                "value": value,
                "updated_at_utc": utc_timestamp(now),
                "description": SETTING_DESCRIPTIONS.get(key, ""),
            }
            existing = by_key.get(key)
            if existing:
                row_number = existing[0]
                batch.append(
                    {
                        "range": (
                            f"A{row_number}:"
                            f"{gspread.utils.rowcol_to_a1(row_number, len(SETTINGS_HEADERS))}"
                        ),
                        "values": [row_values(record, SETTINGS_HEADERS)],
                    }
                )
            else:
                new_rows.append(row_values(record, SETTINGS_HEADERS))
        if batch:
            self._call(
                worksheet.batch_update,
                batch,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        if new_rows:
            self._call(
                worksheet.append_rows,
                new_rows,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )

    def upsert_schedule(
        self,
        games: list[ScheduleGame],
        *,
        now: datetime,
    ) -> tuple[int, int]:
        indexed = self._indexed_records(
            SHEET_TABS["games"], GAME_HEADERS
        )
        by_espn_id = {
            str(record["espn_event_id"]): (row_number, record)
            for row_number, record in indexed
            if record.get("espn_event_id")
        }
        worksheet = self._worksheet(SHEET_TABS["games"])
        updates: list[dict[str, Any]] = []
        new_rows: list[list[Any]] = []
        for game in games:
            existing = by_espn_id.get(game.espn_event_id)
            if existing:
                row_number, record = existing
                merged = merge_schedule_record(record, game, now=now)
                updates.append(
                    {
                        "range": (
                            f"A{row_number}:"
                            f"{gspread.utils.rowcol_to_a1(row_number, len(GAME_MACHINE_HEADERS))}"
                        ),
                        "values": [
                            row_values(merged, GAME_MACHINE_HEADERS)
                        ],
                    }
                )
            else:
                merged = merge_schedule_record(None, game, now=now)
                new_rows.append(row_values(merged, GAME_HEADERS))
        if updates:
            self._call(
                worksheet.batch_update,
                updates,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        if new_rows:
            self._call(
                worksheet.append_rows,
                new_rows,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        self.update_settings(
            {"last_successful_schedule_sync": utc_timestamp(now)},
            now=now,
        )
        return len(new_rows), len(updates)

    @staticmethod
    def _same_game(
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        for key in ("event_id", "espn_event_id"):
            left_value = str(left.get(key) or "")
            right_value = str(right.get(key) or "")
            if left_value and left_value == right_value:
                return True
        return False

    def persist_odds_poll(
        self,
        *,
        due_records: list[dict[str, Any]],
        lines: list[OddsLines],
        requests_used: str,
        requests_remaining: str,
        now: datetime,
    ) -> tuple[int, int]:
        indexed_games = self._indexed_records(
            SHEET_TABS["games"], GAME_HEADERS
        )
        indexed_snapshots = self._indexed_records(
            SHEET_TABS["snapshots"], SNAPSHOT_HEADERS
        )
        latest_snapshot_by_event: dict[str, dict[str, Any]] = {}
        for _, snapshot in indexed_snapshots:
            event_id = str(snapshot.get("event_id") or "")
            if event_id:
                latest_snapshot_by_event[event_id] = snapshot

        changed_rows: dict[int, dict[str, Any]] = {}
        for row_number, record in indexed_games:
            if any(self._same_game(record, due) for due in due_records):
                changed_rows[row_number] = mark_odds_poll(record, now=now)

        snapshot_rows: list[list[Any]] = []
        for line in lines:
            line_identity = asdict(line)
            match = next(
                (
                    (row_number, changed_rows.get(row_number, record))
                    for row_number, record in indexed_games
                    if self._same_game(record, line_identity)
                ),
                None,
            )
            if match is None:
                continue
            row_number, record = match
            changed_rows[row_number] = apply_odds_lines(
                record, line, now=now
            )
            previous = latest_snapshot_by_event.get(line.event_id)
            if should_append_snapshot(line, previous):
                snapshot = snapshot_record(line)
                snapshot_rows.append(
                    row_values(snapshot, SNAPSHOT_HEADERS)
                )
                latest_snapshot_by_event[line.event_id] = snapshot

        games_ws = self._worksheet(SHEET_TABS["games"])
        game_updates = [
            {
                "range": (
                    f"A{row_number}:"
                    f"{gspread.utils.rowcol_to_a1(row_number, len(GAME_MACHINE_HEADERS))}"
                ),
                "values": [row_values(record, GAME_MACHINE_HEADERS)],
            }
            for row_number, record in sorted(changed_rows.items())
        ]
        if game_updates:
            self._call(
                games_ws.batch_update,
                game_updates,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        if snapshot_rows:
            self._call(
                self._worksheet(SHEET_TABS["snapshots"]).append_rows,
                snapshot_rows,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        self.update_settings(
            {
                "last_successful_odds_poll": utc_timestamp(now),
                "last_api_requests_used": requests_used,
                "last_api_requests_remaining": requests_remaining,
            },
            now=now,
        )
        return len(changed_rows), len(snapshot_rows)

    def reconcile_thoughts(
        self,
        *,
        now: datetime,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> tuple[int, int]:
        thoughts = self._indexed_records(
            SHEET_TABS["thoughts"],
            THOUGHT_HEADERS,
            formulas=True,
        )
        updates, unresolved = plan_thought_reconciliation(
            thoughts,
            self.read_games(),
            now=now,
            id_factory=id_factory,
        )
        if updates:
            worksheet = self._worksheet(SHEET_TABS["thoughts"])
            batch = []
            thought_text_column = THOUGHT_HEADERS.index("thought_text") + 1
            for row_number, record in updates:
                frozen_end = gspread.utils.rowcol_to_a1(
                    row_number, len(THOUGHT_FROZEN_HEADERS)
                )
                batch.extend(
                    [
                        {
                            "range": f"A{row_number}:{frozen_end}",
                            "values": [
                                row_values(
                                    record, THOUGHT_FROZEN_HEADERS
                                )
                            ],
                        },
                        {
                            "range": gspread.utils.rowcol_to_a1(
                                row_number, thought_text_column
                            ),
                            "values": [[record["thought_text"]]],
                        },
                    ]
                )
            self._call(
                worksheet.batch_update,
                batch,
                value_input_option=RAW_VALUE_INPUT_OPTION,
            )
        return len(updates), unresolved

    def append_thought(
        self,
        *,
        thought_id: str,
        source: str,
        event_id: str,
        thought_text: str,
        now: datetime,
        telegram_metadata: dict[str, Any] | None = None,
        selection_metadata: dict[str, Any] | None = None,
    ) -> bool:
        created, _ = self.append_thought_record(
            thought_id=thought_id,
            source=source,
            event_id=event_id,
            thought_text=thought_text,
            now=now,
            telegram_metadata=telegram_metadata,
            selection_metadata=selection_metadata,
        )
        return created

    def append_thought_record(
        self,
        *,
        thought_id: str,
        source: str,
        event_id: str,
        thought_text: str,
        now: datetime,
        telegram_metadata: dict[str, Any] | None = None,
        selection_metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        games = self.read_games()
        game = _find_game_for_thought({"event_id": event_id}, games)
        if game is None:
            raise ValueError("No unique WNBA game matches the thought")
        candidate = build_thought_record(
            game,
            thought_id=thought_id,
            thought_text=thought_text,
            source=source,
            now=now,
            telegram_metadata=telegram_metadata,
            selection_metadata=selection_metadata,
        )
        existing = [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["thoughts"],
                THOUGHT_HEADERS,
                formulas=True,
            )
        ]
        duplicate = find_duplicate_thought(candidate, existing)
        if duplicate is not None:
            return False, duplicate
        self._call(
            self._worksheet(SHEET_TABS["thoughts"]).append_rows,
            [row_values(candidate, THOUGHT_HEADERS)],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        return True, candidate

    def read_recent_thoughts(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 20:
            raise ValueError("thought limit must be between 1 and 20")
        records = [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["thoughts"],
                THOUGHT_HEADERS,
                formulas=True,
            )
            if record.get("thought_id")
        ]
        return list(reversed(records[-limit:]))

    def read_snapshots_for_event(
        self,
        *,
        event_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not event_id or len(event_id) > 128:
            raise ValueError("event_id is invalid")
        if limit < 1 or limit > 500:
            raise ValueError("snapshot limit must be between 1 and 500")
        records = [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["snapshots"], SNAPSHOT_HEADERS
            )
            if str(record.get("event_id") or "") == event_id
        ]
        return records[-limit:]

    def read_thoughts_for_event(
        self,
        *,
        event_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not event_id or len(event_id) > 128:
            raise ValueError("event_id is invalid")
        if limit < 1 or limit > 500:
            raise ValueError("thought limit must be between 1 and 500")
        records = [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["thoughts"],
                THOUGHT_HEADERS,
                formulas=True,
            )
            if str(record.get("event_id") or "") == event_id
            and record.get("thought_id")
        ]
        return records[-limit:]

    def read_lean_revision_events(
        self,
        *,
        event_id: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not event_id or len(event_id) > 128:
            raise ValueError("event_id is invalid")
        if limit < 1 or limit > 2000:
            raise ValueError("revision limit must be between 1 and 2000")
        records = [
            record
            for _, record in self._indexed_records(
                SHEET_TABS["lean_revisions"],
                LEAN_REVISION_HEADERS,
                formulas=True,
            )
            if str(record.get("event_id") or "") == event_id
            and record.get("record_id")
        ]
        return records[-limit:]

    def append_lean_revision_event(
        self,
        record: dict[str, Any],
    ) -> bool:
        record_id = str(record.get("record_id") or "")
        event_id = str(record.get("event_id") or "")
        if not record_id or len(record_id) > 300:
            raise ValueError("lean revision record_id is invalid")
        if not event_id or len(event_id) > 128:
            raise ValueError("lean revision event_id is invalid")
        existing = self.read_lean_revision_events(
            event_id=event_id,
            limit=2000,
        )
        if any(
            str(candidate.get("record_id") or "") == record_id
            for candidate in existing
        ):
            return False
        normalized = {
            header: record.get(header, "")
            for header in LEAN_REVISION_HEADERS
        }
        self._call(
            self._worksheet(
                SHEET_TABS["lean_revisions"]
            ).append_rows,
            [row_values(normalized, LEAN_REVISION_HEADERS)],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        return True

    def read_game_history(
        self,
        *,
        event_id: str,
    ) -> dict[str, Any]:
        game = _find_game_for_thought(
            {"event_id": event_id},
            self.read_games(),
        )
        if game is None:
            raise ValueError("No unique WNBA game matches the event_id")
        history = derive_revision_history(
            self.read_lean_revision_events(event_id=event_id),
            event_id=event_id,
        )
        return {
            "game": game,
            "thoughts": self.read_thoughts_for_event(
                event_id=event_id
            ),
            **history,
        }

    def status(self, *, now: datetime) -> dict[str, Any]:
        games = self.read_games()
        settings = {
            str(record.get("key")): record.get("value", "")
            for _, record in self._indexed_records(
                SHEET_TABS["settings"], SETTINGS_HEADERS
            )
        }
        upcoming = [
            game
            for game in games
            if game.get("commence_time_utc")
            and parse_timestamp(str(game["commence_time_utc"])) > now
        ]
        return {
            "games": len(games),
            "upcoming_games": len(upcoming),
            "due_games": len(due_games(games, now)),
            "last_successful_schedule_sync": settings.get(
                "last_successful_schedule_sync", ""
            ),
            "last_successful_odds_poll": settings.get(
                "last_successful_odds_poll", ""
            ),
            "last_api_requests_used": settings.get(
                "last_api_requests_used", ""
            ),
            "last_api_requests_remaining": settings.get(
                "last_api_requests_remaining", ""
            ),
        }
