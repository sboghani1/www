from datetime import datetime, timedelta, timezone

from wnba_poller.models import LINE_FIELDS, OddsLines, ScheduleGame
from wnba_poller.sheets import (
    ALLOWED_USER_HEADERS,
    RAW_VALUE_INPUT_OPTION,
    SheetsStore,
    THOUGHT_HEADERS,
    apply_odds_lines,
    build_thought_record,
    is_duplicate_thought,
    line_cell,
    merge_schedule_record,
    normalize_sheet_row,
    plan_thought_reconciliation,
    should_append_snapshot,
    snapshot_record,
)

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def _lines(
    *,
    captured_at: datetime = NOW,
    away_spread: float = 4.5,
    first_half_away_spread: float | None = 2.5,
) -> OddsLines:
    return OddsLines(
        captured_at_utc=captured_at.isoformat().replace("+00:00", "Z"),
        event_id="odds-1",
        espn_event_id="espn-1",
        commence_time_utc="2026-08-05T23:00:00Z",
        commence_time_et="2026-08-05T19:00:00-04:00",
        away_team="Atlanta Dream",
        home_team="New York Liberty",
        bookmaker="BetOnline.ag",
        away_spread=away_spread,
        away_spread_price=-110,
        away_moneyline=150,
        home_spread=-away_spread,
        home_spread_price=-110,
        home_moneyline=-175,
        total=164.5,
        over_price=-105,
        under_price=-115,
        first_half_away_spread=first_half_away_spread,
        first_half_away_spread_price=(
            -112 if first_half_away_spread is not None else None
        ),
        first_half_home_spread=(
            -first_half_away_spread
            if first_half_away_spread is not None
            else None
        ),
        first_half_home_spread_price=(
            -108 if first_half_away_spread is not None else None
        ),
        first_half_total=(
            82.5 if first_half_away_spread is not None else None
        ),
        first_half_over_price=(
            -110 if first_half_away_spread is not None else None
        ),
        first_half_under_price=(
            -110 if first_half_away_spread is not None else None
        ),
        api_requests_used="11",
        api_requests_remaining="489",
    )


def test_schedule_upsert_preserves_manual_and_line_fields() -> None:
    existing = {
        "event_id": "odds-1",
        "opening_away_spread": 3.5,
        "latest_away_spread": 4.0,
        "manual_status": "watch",
        "user_notes": "=keep this exact note",
    }
    schedule = ScheduleGame(
        espn_event_id="espn-1",
        status="scheduled",
        commence_time_utc="2026-08-05T23:00:00Z",
        commence_time_et="2026-08-05T19:00:00-04:00",
        away_team="Atlanta Dream",
        home_team="New York Liberty",
        venue="Arena",
        broadcast="ESPN",
    )

    merged = merge_schedule_record(existing, schedule, now=NOW)

    assert merged["event_id"] == "odds-1"
    assert merged["opening_away_spread"] == 3.5
    assert merged["latest_away_spread"] == 4.0
    assert merged["manual_status"] == "watch"
    assert merged["user_notes"] == "=keep this exact note"


def test_opening_is_preserved_while_latest_changes() -> None:
    first = apply_odds_lines({}, _lines(away_spread=4.5), now=NOW)
    later = apply_odds_lines(
        first,
        _lines(
            captured_at=NOW + timedelta(minutes=15),
            away_spread=5.0,
        ),
        now=NOW + timedelta(minutes=15),
    )

    assert later["opening_away_spread"] == 4.5
    assert later["latest_away_spread"] == 5.0
    assert later["opening_captured_at"] == "2026-08-05T16:00:00Z"
    assert later["latest_captured_at"] == "2026-08-05T16:15:00Z"


def test_missing_first_half_is_nodata_and_opening_fills_when_available() -> None:
    first = apply_odds_lines(
        {}, _lines(first_half_away_spread=None), now=NOW
    )
    later = apply_odds_lines(
        first,
        _lines(captured_at=NOW + timedelta(minutes=15)),
        now=NOW + timedelta(minutes=15),
    )

    assert first["latest_first_half_away_spread"] == "nodata"
    assert later["opening_first_half_away_spread"] == 2.5
    assert later["latest_first_half_away_spread"] == 2.5


def test_snapshot_changed_immediately_and_identical_only_hourly() -> None:
    first_lines = _lines()
    previous = snapshot_record(first_lines)

    assert not should_append_snapshot(
        _lines(captured_at=NOW + timedelta(minutes=59)),
        previous,
    )
    assert should_append_snapshot(
        _lines(captured_at=NOW + timedelta(minutes=60)),
        previous,
    )
    assert should_append_snapshot(
        _lines(
            captured_at=NOW + timedelta(minutes=1),
            away_spread=5.0,
        ),
        previous,
    )


def test_formula_text_is_preserved_and_writes_are_forced_raw() -> None:
    row = normalize_sheet_row(["thought_text"], ["=1+1"])

    assert row["thought_text"] == "=1+1"
    assert RAW_VALUE_INPUT_OPTION == "RAW"
    assert line_cell(None) == "nodata"


def test_direct_sheet_thought_gets_immutable_frozen_metadata() -> None:
    game = apply_odds_lines({}, _lines(), now=NOW)
    thought = {header: "" for header in THOUGHT_HEADERS}
    thought.update(
        {
            "event_id": "odds-1",
            "thought_text": "=HYPERLINK(\"https://example.invalid\",\"lean\")",
        }
    )

    updates, unresolved = plan_thought_reconciliation(
        [(2, thought)],
        [game],
        now=NOW,
        id_factory=lambda: "thought-fixed",
    )

    assert unresolved == 0
    row_number, reconciled = updates[0]
    assert row_number == 2
    assert reconciled["thought_id"] == "thought-fixed"
    assert reconciled["source"] == "sheet"
    assert reconciled["submitted_at_utc"] == "2026-08-05T16:00:00Z"
    assert reconciled["submitted_at_et"] == "2026-08-05T12:00:00-04:00"
    assert reconciled["frozen_away_spread"] == 4.5
    assert reconciled["thought_text"] == thought["thought_text"]
    assert set(f"frozen_{field}" for field in LINE_FIELDS).issubset(
        reconciled
    )

    second_updates, _ = plan_thought_reconciliation(
        [(2, reconciled)],
        [game],
        now=NOW + timedelta(minutes=1),
    )
    assert second_updates == []


def test_telegram_thought_record_is_frozen_and_idempotent() -> None:
    game = apply_odds_lines({}, _lines(), now=NOW)
    record = build_thought_record(
        game,
        thought_id="telegram:123:456",
        thought_text="+Dream first half",
        source="telegram",
        now=NOW,
        telegram_metadata={
            "telegram_user_id": "123",
            "telegram_chat_id": "123",
            "telegram_message_id": "456",
        },
        selection_metadata={
            "period": "first_half",
            "market": "total",
            "side": "Under",
            "opening_selected_line": 82.5,
            "opening_selected_price": -110,
            "latest_selected_line": 82.5,
            "latest_selected_price": -110,
        },
    )

    assert record["thought_text"] == "+Dream first half"
    assert record["source"] == "telegram"
    assert record["frozen_first_half_away_spread"] == 2.5
    assert record["period"] == "first_half"
    assert record["market"] == "total"
    assert record["side"] == "Under"
    assert record["latest_selected_line"] == 82.5
    assert is_duplicate_thought(record, [record])

    retry_with_new_id = dict(record)
    retry_with_new_id["thought_id"] = "another-id"
    assert is_duplicate_thought(retry_with_new_id, [record])


def test_allowed_user_seed_is_raw_and_idempotent() -> None:
    class Worksheet:
        def __init__(self) -> None:
            self.rows = []
            self.value_input_option = None

        def append_rows(self, rows, *, value_input_option):
            self.rows.extend(rows)
            self.value_input_option = value_input_option

    worksheet = Worksheet()
    store = SheetsStore(None)
    store.read_allowed_users = lambda: []
    store._worksheet = lambda tab_name: worksheet

    created = store.seed_allowed_user(
        user_id=123,
        username="guesser",
        display_name="WNBA Guesser",
        now=NOW,
    )

    assert created
    assert worksheet.value_input_option == RAW_VALUE_INPUT_OPTION
    assert worksheet.rows[0][ALLOWED_USER_HEADERS.index("telegram_user_id")] == 123
    assert worksheet.rows[0][ALLOWED_USER_HEADERS.index("enabled")] == "true"
    store.read_allowed_users = lambda: [{"telegram_user_id": "123"}]
    assert not store.seed_allowed_user(
        user_id=123,
        username="changed",
        display_name="Changed",
        now=NOW,
    )
    assert len(worksheet.rows) == 1
