from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

TAB_NAME = "nfl_leans"
BACKUP_TAB = "nfl_leans_backup_20260901"
TARGET_USER_ID = "6780239459"
CONFIRMATION = "CORRECT-6780239459-20260901"

HEADERS = [
    "submission_id",
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "telegram_first_name",
    "telegram_last_name",
    "telegram_message_id",
    "event_id",
    "season",
    "season_type",
    "week",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    "period",
    "market",
    "side",
    "opening_captured_at",
    "latest_captured_at",
    "opening_selected_line",
    "opening_selected_price",
    "latest_selected_line",
    "latest_selected_price",
    (
        "opening_away_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "opening_home_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "opening_totals_game_total_overprice_underprice__"
        "h1_total_overprice_underprice__"
        "q1_total_overprice_underprice"
    ),
    (
        "latest_away_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "latest_home_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "latest_totals_game_total_overprice_underprice__"
        "h1_total_overprice_underprice__"
        "q1_total_overprice_underprice"
    ),
    "lean_text",
]


@dataclass(frozen=True)
class ExpectedRow:
    event_id: str
    period: str
    market: str
    side: str


DELETE_ROWS = {
    "telegram:6780239459:20": ExpectedRow(
        "ae7d5615fc569079db13e4fcafc05a5d",
        "first_quarter",
        "total",
        "Under",
    ),
    "telegram:6780239459:37": ExpectedRow(
        "ae7d5615fc569079db13e4fcafc05a5d",
        "game",
        "total",
        "Over",
    ),
    "telegram:6780239459:44": ExpectedRow(
        "ae7d5615fc569079db13e4fcafc05a5d",
        "game",
        "total",
        "Under",
    ),
    "telegram:6780239459:49": ExpectedRow(
        "ae7d5615fc569079db13e4fcafc05a5d",
        "game",
        "spread",
        "Carolina Panthers",
    ),
    "telegram:6780239459:67": ExpectedRow(
        "8c94552d022acec4a0458d70c19d3da9",
        "game",
        "spread",
        "Seattle Seahawks",
    ),
    "telegram:6780239459:87": ExpectedRow(
        "8c94552d022acec4a0458d70c19d3da9",
        "first_half",
        "total",
        "Over",
    ),
    "telegram:6780239459:117": ExpectedRow(
        "8c94552d022acec4a0458d70c19d3da9",
        "game",
        "spread",
        "Seattle Seahawks",
    ),
}

KEEP_ROWS = {
    "telegram:6780239459:76": ExpectedRow(
        "8c94552d022acec4a0458d70c19d3da9",
        "game",
        "moneyline",
        "Seattle Seahawks",
    ),
    "telegram:6780239459:92": ExpectedRow(
        "8c94552d022acec4a0458d70c19d3da9",
        "first_half",
        "moneyline",
        "Seattle Seahawks",
    ),
}

CONVERT_ROWS = {
    "telegram:6780239459:153": ExpectedRow(
        "acc580d74344ea3b31bbcdd057fe6a9c",
        "game",
        "spread",
        "Los Angeles Rams",
    ),
    "telegram:6780239459:188": ExpectedRow(
        "95c01d1bb797d6df14824b106c5a9130",
        "game",
        "spread",
        "Pittsburgh Steelers",
    ),
    "telegram:6780239459:194": ExpectedRow(
        "b6cfdcbafa61ce220ba87dc2d9b80c77",
        "game",
        "spread",
        "Baltimore Ravens",
    ),
    "telegram:6780239459:202": ExpectedRow(
        "7e09efed7e12c659b82740b67ce2f9a1",
        "game",
        "spread",
        "Buffalo Bills",
    ),
    "telegram:6780239459:208": ExpectedRow(
        "fc362aff0d889ec52d358307a70c32ed",
        "game",
        "spread",
        "Chicago Bears",
    ),
    "telegram:6780239459:214": ExpectedRow(
        "5ad8135dc2b5f27de0b777acd317855a",
        "game",
        "spread",
        "Kansas City Chiefs",
    ),
    "telegram:6780239459:279": ExpectedRow(
        "ed6d24ff979f9c71979fead577b0b3f7",
        "game",
        "spread",
        "Cincinnati Bengals",
    ),
    "telegram:6780239459:283": ExpectedRow(
        "e55c6fe19fce094ce214c8b0e5b504e9",
        "game",
        "spread",
        "Jacksonville Jaguars",
    ),
    "telegram:6780239459:287": ExpectedRow(
        "7dddb296a42e7a41a774b24bd1709ce1",
        "game",
        "spread",
        "Tennessee Titans",
    ),
}

EXPECTED_PRICES = {
    "telegram:6780239459:153": ("-190", "-195"),
    "telegram:6780239459:188": ("-160", "-160"),
    "telegram:6780239459:194": ("-185", "-180"),
    "telegram:6780239459:202": ("nodata", "nodata"),
    "telegram:6780239459:208": ("-145", "-150"),
    "telegram:6780239459:214": ("-148", "-140"),
    "telegram:6780239459:279": ("-190", "-210"),
    "telegram:6780239459:283": ("-360", "-400"),
    "telegram:6780239459:287": ("-150", "-130"),
}
PENDING_TARGET_IDS = set(DELETE_ROWS) | set(KEEP_ROWS) | set(CONVERT_ROWS)
APPLIED_TARGET_IDS = set(KEEP_ROWS) | set(CONVERT_ROWS)


def _string(value: Any) -> str:
    return str(value if value is not None else "")


def _assert_expected(
    submission_id: str,
    row: dict[str, Any],
    expected: ExpectedRow,
    *,
    allow_converted: bool = False,
) -> None:
    actual_user_id = _string(row.get("telegram_user_id"))
    actual = (
        _string(row.get("event_id")),
        _string(row.get("period")),
        _string(row.get("market")),
        _string(row.get("side")),
    )
    expected_values = (
        expected.event_id,
        expected.period,
        expected.market,
        expected.side,
    )
    converted_values = (
        expected.event_id,
        expected.period,
        "moneyline",
        expected.side,
    )
    if actual_user_id != TARGET_USER_ID or (
        actual != expected_values
        and not (allow_converted and actual == converted_values)
    ):
        raise ValueError(
            f"{submission_id} no longer matches its guarded precondition"
        )


def _snapshot_moneyline(row: dict[str, Any], prefix: str) -> str:
    side = _string(row["side"])
    if side == _string(row["away_team"]):
        team = "away"
    elif side == _string(row["home_team"]):
        team = "home"
    else:
        raise ValueError(
            f"{row['submission_id']} side does not match either team"
        )
    column = next(
        header
        for header in HEADERS
        if header.startswith(f"{prefix}_{team}_game_")
    )
    periods = _string(row[column]).split("|")
    period_index = {"game": 0, "first_half": 1, "first_quarter": 2}.get(
        _string(row["period"])
    )
    if period_index is None or len(periods) <= period_index:
        raise ValueError(
            f"{row['submission_id']} has an invalid period snapshot"
        )
    fields = periods[period_index].split(",")
    if len(fields) != 3:
        raise ValueError(
            f"{row['submission_id']} has an invalid team snapshot"
        )
    return fields[2]


def _rows_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        submission_id = _string(row.get("submission_id"))
        if not submission_id:
            raise ValueError("nfl_leans contains a row without submission_id")
        if submission_id in by_id:
            raise ValueError(
                f"nfl_leans contains duplicate submission_id {submission_id}"
            )
        by_id[submission_id] = row
    return by_id


def correction_state(rows: list[dict[str, Any]]) -> str:
    by_id = _rows_by_id(rows)
    target_ids = {
        submission_id
        for submission_id, row in by_id.items()
        if _string(row.get("telegram_user_id")) == TARGET_USER_ID
    }
    kept = set(KEEP_ROWS) <= set(by_id)
    deletions_present = set(DELETE_ROWS) <= set(by_id)
    deletions_absent = not (set(DELETE_ROWS) & set(by_id))
    conversions_present = set(CONVERT_ROWS) <= set(by_id)
    if not kept or not conversions_present:
        raise ValueError("Guarded NFL lean rows are missing")
    for submission_id, expected in KEEP_ROWS.items():
        _assert_expected(submission_id, by_id[submission_id], expected)
    conversion_markets = {
        _string(by_id[submission_id].get("market"))
        for submission_id in CONVERT_ROWS
    }
    if (
        deletions_present
        and conversion_markets == {"spread"}
        and target_ids == PENDING_TARGET_IDS
    ):
        return "pending"
    if (
        deletions_absent
        and conversion_markets == {"moneyline"}
        and target_ids == APPLIED_TARGET_IDS
    ):
        for submission_id, expected in CONVERT_ROWS.items():
            row = by_id[submission_id]
            _assert_expected(
                submission_id, row, expected, allow_converted=True
            )
            if (
                _string(row["opening_selected_line"]) != "nodata"
                or _string(row["latest_selected_line"]) != "nodata"
                or _string(row["opening_selected_price"])
                != _snapshot_moneyline(row, "opening")
                or _string(row["latest_selected_price"])
                != _snapshot_moneyline(row, "latest")
            ):
                raise ValueError(
                    f"{submission_id} has an incomplete moneyline conversion"
                )
        return "applied"
    raise ValueError(
        "NFL lean correction source set is incomplete, expanded, "
        "partially applied, or divergent"
    )


def transform_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if correction_state(rows) == "applied":
        return rows, []
    by_id = _rows_by_id(rows)
    for submission_id, expected in DELETE_ROWS.items():
        _assert_expected(submission_id, by_id[submission_id], expected)
    for submission_id, expected in CONVERT_ROWS.items():
        _assert_expected(submission_id, by_id[submission_id], expected)

    changes: list[dict[str, str]] = []
    transformed: list[dict[str, Any]] = []
    for source_row in rows:
        submission_id = _string(source_row["submission_id"])
        if submission_id in DELETE_ROWS:
            changes.append(
                {
                    "submission_id": submission_id,
                    "operation": "delete",
                    "event_id": _string(source_row["event_id"]),
                    "from": (
                        f"{source_row['period']} {source_row['market']} "
                        f"{source_row['side']}"
                    ),
                    "to": "",
                }
            )
            continue
        row = dict(source_row)
        if submission_id in CONVERT_ROWS:
            opening_price = _snapshot_moneyline(row, "opening")
            latest_price = _snapshot_moneyline(row, "latest")
            if (opening_price, latest_price) != EXPECTED_PRICES[submission_id]:
                raise ValueError(
                    f"{submission_id} captured moneylines no longer match "
                    "the guarded source snapshot"
                )
            row["market"] = "moneyline"
            row["opening_selected_line"] = "nodata"
            row["latest_selected_line"] = "nodata"
            row["opening_selected_price"] = opening_price
            row["latest_selected_price"] = latest_price
            changes.append(
                {
                    "submission_id": submission_id,
                    "operation": "convert",
                    "event_id": _string(row["event_id"]),
                    "from": f"spread {source_row['side']}",
                    "to": (
                        f"moneyline {row['side']} "
                        f"({opening_price} -> {latest_price})"
                    ),
                }
            )
        transformed.append(row)
    if len(rows) - len(transformed) != len(DELETE_ROWS):
        raise ValueError("Unexpected NFL lean deletion count")
    if correction_state(transformed) != "applied":
        raise ValueError("Transformed NFL lean rows failed validation")
    return transformed, changes


def _credentials() -> Credentials:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    encoded = os.getenv("GOOGLE_CREDENTIALS")
    credentials_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if encoded:
        info = json.loads(base64.b64decode(encoded))
        return Credentials.from_service_account_info(info, scopes=scopes)
    if credentials_file:
        return Credentials.from_service_account_file(
            credentials_file, scopes=scopes
        )
    raise ValueError("Google service account credentials are unavailable")


def _load_rows(
    worksheet: gspread.Worksheet,
) -> tuple[list[list[str]], list[dict[str, str]]]:
    values = worksheet.get_all_values()
    if not values or values[0] != HEADERS:
        raise ValueError(f"{TAB_NAME} headers do not match the guarded schema")
    rows = [
        dict(zip(HEADERS, row + [""] * (len(HEADERS) - len(row))))
        for row in values[1:]
        if any(row)
    ]
    return values, rows


def _open_sheet() -> tuple[gspread.Spreadsheet, gspread.Worksheet]:
    sheet_id = os.getenv("NFL_INTAKE_SHEET_ID")
    if not sheet_id:
        raise ValueError("NFL_INTAKE_SHEET_ID is required")
    spreadsheet = gspread.authorize(_credentials()).open_by_key(sheet_id)
    return spreadsheet, spreadsheet.worksheet(TAB_NAME)


def preview() -> dict[str, Any]:
    _, worksheet = _open_sheet()
    _, rows = _load_rows(worksheet)
    state = correction_state(rows)
    transformed, changes = transform_rows(rows)
    return {
        "status": state,
        "sheet": TAB_NAME,
        "rows_before": len(rows),
        "rows_after": len(transformed),
        "changes": changes,
    }


def _cell_value(value: str) -> dict[str, dict[str, Any]]:
    try:
        number = int(value)
    except ValueError:
        return {"userEnteredValue": {"stringValue": value}}
    return {"userEnteredValue": {"numberValue": number}}


def _update_request(
    *,
    sheet_id: int,
    row_index: int,
    column_index: int,
    values: list[str],
) -> dict[str, Any]:
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_index,
                "endRowIndex": row_index + 1,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + len(values),
            },
            "rows": [{"values": [_cell_value(value) for value in values]}],
            "fields": "userEnteredValue",
        }
    }


def _batch_requests(
    old_values: list[list[str]],
    worksheet: gspread.Worksheet,
    transformed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    transformed_by_id = _rows_by_id(transformed)
    row_indexes = {
        row[0]: index
        for index, row in enumerate(old_values)
        if index > 0 and row
    }
    requests: list[dict[str, Any]] = []
    for submission_id in CONVERT_ROWS:
        row_index = row_indexes[submission_id]
        row = transformed_by_id[submission_id]
        requests.extend(
            [
                _update_request(
                    sheet_id=worksheet.id,
                    row_index=row_index,
                    column_index=HEADERS.index("market"),
                    values=["moneyline"],
                ),
                _update_request(
                    sheet_id=worksheet.id,
                    row_index=row_index,
                    column_index=HEADERS.index("opening_selected_line"),
                    values=[
                        "nodata",
                        _string(row["opening_selected_price"]),
                    ],
                ),
                _update_request(
                    sheet_id=worksheet.id,
                    row_index=row_index,
                    column_index=HEADERS.index("latest_selected_line"),
                    values=[
                        "nodata",
                        _string(row["latest_selected_price"]),
                    ],
                ),
            ]
        )
    for submission_id in sorted(
        DELETE_ROWS, key=lambda value: row_indexes[value], reverse=True
    ):
        row_index = row_indexes[submission_id]
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "ROWS",
                        "startIndex": row_index,
                        "endIndex": row_index + 1,
                    }
                }
            }
        )
    return requests


def _row_values(rows: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [_string(row.get(header)) for header in HEADERS]
        for row in rows
    ]


def apply() -> dict[str, Any]:
    spreadsheet, worksheet = _open_sheet()
    old_values, rows = _load_rows(worksheet)
    state = correction_state(rows)
    if state == "applied":
        return {
            "status": "already_applied",
            "sheet": TAB_NAME,
            "rows": len(rows),
        }
    transformed, changes = transform_rows(rows)
    existing_backup = next(
        (
            sheet
            for sheet in spreadsheet.worksheets()
            if sheet.title == BACKUP_TAB
        ),
        None,
    )
    if existing_backup:
        if existing_backup.get_all_values() != old_values:
            raise ValueError(
                f"Existing backup tab {BACKUP_TAB} does not match "
                "the pending source"
            )
        backup = existing_backup
    else:
        backup = spreadsheet.duplicate_sheet(
            source_sheet_id=worksheet.id,
            new_sheet_name=BACKUP_TAB,
        )
        backup.hide()

    requests = _batch_requests(old_values, worksheet, transformed)
    try:
        spreadsheet.batch_update({"requests": requests})
        _, readback = _load_rows(worksheet)
        if (
            correction_state(readback) != "applied"
            or _row_values(readback) != _row_values(transformed)
        ):
            raise RuntimeError("Correction readback did not match the plan")
    except Exception as error:
        try:
            backup.show()
        except Exception:
            pass
        raise RuntimeError(
            f"Correction failed; source backup is {BACKUP_TAB}: {error}"
        ) from error
    return {
        "status": "applied",
        "applied_at_utc": datetime.now(UTC).isoformat(),
        "sheet": TAB_NAME,
        "backup_sheet": BACKUP_TAB,
        "rows_before": len(rows),
        "rows_after": len(readback),
        "changes": changes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the guarded September 2026 NFL lean correction."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preview")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--confirm", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "preview":
            result = preview()
        else:
            if arguments.confirm != CONFIRMATION:
                raise ValueError("Confirmation token does not match")
            result = apply()
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        print(
            json.dumps(
                {"status": "error", "error": str(error)},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
