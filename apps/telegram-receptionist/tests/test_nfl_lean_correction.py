from __future__ import annotations

from copy import deepcopy

import pytest

from receptionist.nfl_lean_correction import (
    _batch_requests,
    CONVERT_ROWS,
    DELETE_ROWS,
    EXPECTED_PRICES,
    HEADERS,
    KEEP_ROWS,
    TARGET_USER_ID,
    correction_state,
    transform_rows,
)

AWAY_SIDE_IDS = {
    "telegram:6780239459:194",
    "telegram:6780239459:202",
    "telegram:6780239459:208",
}


def _row(
    submission_id: str,
    event_id: str,
    period: str,
    market: str,
    side: str,
) -> dict[str, str]:
    row = dict.fromkeys(HEADERS, "")
    row.update(
        {
            "submission_id": submission_id,
            "telegram_user_id": TARGET_USER_ID,
            "event_id": event_id,
            "period": period,
            "market": market,
            "side": side,
            "away_team": (
                side if submission_id in AWAY_SIDE_IDS else "Away Team"
            ),
            "home_team": (
                side if submission_id not in AWAY_SIDE_IDS else "Home Team"
            ),
            "opening_selected_line": "-3.5",
            "opening_selected_price": "-110",
            "latest_selected_line": "-4",
            "latest_selected_price": "-105",
        }
    )
    opening_price, latest_price = EXPECTED_PRICES.get(
        submission_id, ("165", "-190")
    )
    selected_opening = f"3.5,-110,{opening_price}"
    selected_latest = f"4.0,-105,{latest_price}"
    other_opening = "3.5,-110,125"
    other_latest = "4.0,-105,130"
    for header in HEADERS:
        if header.startswith("opening_away_game_"):
            group = (
                selected_opening
                if submission_id in AWAY_SIDE_IDS
                else other_opening
            )
            row[header] = f"{group}|nodata,nodata,nodata|nodata,nodata,nodata"
        elif header.startswith("opening_home_game_"):
            group = (
                other_opening
                if submission_id in AWAY_SIDE_IDS
                else selected_opening
            )
            row[header] = f"{group}|nodata,nodata,nodata|nodata,nodata,nodata"
        elif header.startswith("latest_away_game_"):
            group = (
                selected_latest
                if submission_id in AWAY_SIDE_IDS
                else other_latest
            )
            row[header] = f"{group}|nodata,nodata,nodata|nodata,nodata,nodata"
        elif header.startswith("latest_home_game_"):
            group = (
                other_latest
                if submission_id in AWAY_SIDE_IDS
                else selected_latest
            )
            row[header] = f"{group}|nodata,nodata,nodata|nodata,nodata,nodata"
    return row


def _pending_rows() -> list[dict[str, str]]:
    rows = [
        _row(
            submission_id,
            expected.event_id,
            expected.period,
            expected.market,
            expected.side,
        )
        for submission_id, expected in {
            **DELETE_ROWS,
            **KEEP_ROWS,
            **CONVERT_ROWS,
        }.items()
    ]
    unrelated = dict.fromkeys(HEADERS, "")
    unrelated.update(
        {
            "submission_id": "telegram:other:1",
            "telegram_user_id": "other",
            "market": "spread",
        }
    )
    rows.append(unrelated)
    return rows


def test_transform_deletes_and_converts_only_guarded_rows() -> None:
    rows = _pending_rows()
    transformed, changes = transform_rows(rows)
    by_id = {row["submission_id"]: row for row in transformed}

    assert correction_state(rows) == "pending"
    assert correction_state(transformed) == "applied"
    assert not (set(DELETE_ROWS) & set(by_id))
    assert by_id["telegram:other:1"]["market"] == "spread"
    assert len(changes) == len(DELETE_ROWS) + len(CONVERT_ROWS)
    assert sum(change["operation"] == "delete" for change in changes) == 7
    assert sum(change["operation"] == "convert" for change in changes) == 9

    for submission_id, expected in CONVERT_ROWS.items():
        row = by_id[submission_id]
        assert row["market"] == "moneyline"
        assert row["side"] == expected.side
        assert row["opening_selected_line"] == "nodata"
        assert row["latest_selected_line"] == "nodata"
        expected_open, expected_latest = EXPECTED_PRICES[submission_id]
        assert row["opening_selected_price"] == expected_open
        assert row["latest_selected_price"] == expected_latest

    for submission_id in KEEP_ROWS:
        assert by_id[submission_id] == next(
            row for row in rows if row["submission_id"] == submission_id
        )


def test_transform_is_idempotent_after_application() -> None:
    transformed, _ = transform_rows(_pending_rows())
    repeated, changes = transform_rows(transformed)

    assert repeated == transformed
    assert changes == []


def test_transform_rejects_divergent_guarded_row() -> None:
    rows = deepcopy(_pending_rows())
    row = next(
        row
        for row in rows
        if row["submission_id"] == "telegram:6780239459:153"
    )
    row["side"] = "San Francisco 49ers"

    with pytest.raises(ValueError, match="guarded precondition"):
        transform_rows(rows)


def test_transform_rejects_unexpected_target_user_row() -> None:
    rows = _pending_rows()
    extra = dict.fromkeys(HEADERS, "")
    extra.update(
        {
            "submission_id": "telegram:6780239459:999",
            "telegram_user_id": TARGET_USER_ID,
            "market": "spread",
        }
    )
    rows.append(extra)

    with pytest.raises(ValueError, match="source set"):
        transform_rows(rows)


def test_transform_rejects_changed_snapshot_price() -> None:
    rows = _pending_rows()
    row = next(
        row
        for row in rows
        if row["submission_id"] == "telegram:6780239459:153"
    )
    header = next(
        header for header in HEADERS if header.startswith("latest_home_game_")
    )
    row[header] = "-4,-105,-200|nodata,nodata,nodata|nodata,nodata,nodata"

    with pytest.raises(ValueError, match="captured moneylines"):
        transform_rows(rows)


def test_batch_request_updates_then_deletes_in_descending_order() -> None:
    rows = _pending_rows()
    transformed, _ = transform_rows(rows)
    old_values = [HEADERS] + [
        [row[header] for header in HEADERS] for row in rows
    ]
    worksheet = type("Worksheet", (), {"id": 123})()

    requests = _batch_requests(old_values, worksheet, transformed)

    assert len(requests) == len(CONVERT_ROWS) * 3 + len(DELETE_ROWS)
    assert all("updateCells" in request for request in requests[:27])
    delete_indexes = [
        request["deleteDimension"]["range"]["startIndex"]
        for request in requests[27:]
    ]
    assert delete_indexes == sorted(delete_indexes, reverse=True)
    assert requests[0]["updateCells"]["rows"][0]["values"] == [
        {"userEnteredValue": {"stringValue": "moneyline"}}
    ]
