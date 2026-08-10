from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wnba_poller.sheets import (
    ALLOWED_USER_HEADERS,
    GAME_HEADERS,
    LEGACY_NFL_TABS,
    RAW_VALUE_INPUT_OPTION,
    RESULT_HEADERS,
    SETTINGS_HEADERS,
    SHEET_TABS,
    SNAPSHOT_HEADERS,
    SheetsStore,
    TAB_HEADERS,
    THOUGHT_HEADERS,
)
from wnba_poller.lean_revisions import LEAN_REVISION_HEADERS

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)


class FakeWorksheet:
    def __init__(
        self,
        title: str,
        *,
        worksheet_id: int,
        headers: list[str] | None = None,
        rows: list[list[Any]] | None = None,
    ) -> None:
        self.title = title
        self.id = worksheet_id
        self.row_count = 2000
        self._grid: list[list[Any]] = []
        if headers is not None:
            self._grid.append(list(headers))
        if rows:
            self._grid.extend(list(row) for row in rows)
        self.col_count = len(headers) if headers else 0

    def get_all_values(self, value_render_option: str | None = None) -> list[list[Any]]:
        return [list(row) for row in self._grid]

    def update(
        self,
        range_name: str,
        values: list[list[Any]],
        value_input_option: str | None = None,
    ) -> None:
        assert range_name == "A1"
        assert value_input_option == RAW_VALUE_INPUT_OPTION
        for index, row in enumerate(values):
            if index < len(self._grid):
                self._grid[index] = list(row)
            else:
                self._grid.append(list(row))

    def append_rows(
        self, rows: list[list[Any]], *, value_input_option: str | None = None
    ) -> None:
        assert value_input_option == RAW_VALUE_INPUT_OPTION
        self._grid.extend(list(row) for row in rows)

    def batch_update(
        self, batch: list[dict[str, Any]], value_input_option: str | None = None
    ) -> None:
        assert value_input_option == RAW_VALUE_INPUT_OPTION
        for item in batch:
            row_number = int(item["range"].split(":")[0][1:])
            while len(self._grid) < row_number:
                self._grid.append([])
            self._grid[row_number - 1] = list(item["values"][0])


class FakeSpreadsheet:
    def __init__(self, worksheets: list[FakeWorksheet] | None = None) -> None:
        self._worksheets = {ws.title: ws for ws in (worksheets or [])}
        self._next_id = 1000
        self.title = "asce Guesser"

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self._worksheets.values())

    def worksheet(self, title: str) -> FakeWorksheet:
        return self._worksheets[title]

    def add_worksheet(self, *, title: str, rows: int, cols: int) -> FakeWorksheet:
        self._next_id += 1
        worksheet = FakeWorksheet(title, worksheet_id=self._next_id)
        worksheet.row_count = rows
        worksheet.col_count = cols
        self._worksheets[title] = worksheet
        return worksheet

    def del_worksheet(self, worksheet: FakeWorksheet) -> None:
        self._worksheets.pop(worksheet.title, None)


def _fully_seeded_spreadsheet() -> FakeSpreadsheet:
    worksheets = [
        FakeWorksheet("wnba_games", worksheet_id=1, headers=GAME_HEADERS),
        FakeWorksheet("wnba_line_snapshots", worksheet_id=2, headers=SNAPSHOT_HEADERS),
        FakeWorksheet("wnba_thoughts", worksheet_id=3, headers=THOUGHT_HEADERS),
        FakeWorksheet(
            "wnba_lean_revisions", worksheet_id=4, headers=LEAN_REVISION_HEADERS
        ),
        FakeWorksheet(
            "wnba_allowed_users", worksheet_id=5, headers=ALLOWED_USER_HEADERS
        ),
        FakeWorksheet("wnba_settings", worksheet_id=6, headers=SETTINGS_HEADERS),
        FakeWorksheet("wnba_results", worksheet_id=7, headers=RESULT_HEADERS),
    ]
    return FakeSpreadsheet(worksheets)


class TestExpectedSchema:
    def test_tab_headers_covers_exactly_the_plan_schema(self) -> None:
        assert set(TAB_HEADERS) == {
            "wnba_games",
            "wnba_line_snapshots",
            "wnba_thoughts",
            "wnba_lean_revisions",
            "wnba_allowed_users",
            "wnba_settings",
            "wnba_results",
        }
        assert TAB_HEADERS["wnba_games"] == GAME_HEADERS
        assert TAB_HEADERS["wnba_line_snapshots"] == SNAPSHOT_HEADERS
        assert TAB_HEADERS["wnba_thoughts"] == THOUGHT_HEADERS
        assert TAB_HEADERS["wnba_lean_revisions"] == LEAN_REVISION_HEADERS
        assert TAB_HEADERS["wnba_allowed_users"] == ALLOWED_USER_HEADERS
        assert TAB_HEADERS["wnba_settings"] == SETTINGS_HEADERS
        assert TAB_HEADERS["wnba_results"] == RESULT_HEADERS

    def test_games_headers_include_every_plan_line_field_opening_and_latest(
        self,
    ) -> None:
        assert "event_id" in GAME_HEADERS
        assert "espn_event_id" in GAME_HEADERS
        assert "opening_away_spread" in GAME_HEADERS
        assert "latest_away_spread" in GAME_HEADERS
        assert "opening_first_half_total" in GAME_HEADERS
        assert "latest_first_half_total" in GAME_HEADERS
        assert "next_poll_at" in GAME_HEADERS
        assert "user_notes" in GAME_HEADERS

    def test_lean_revision_headers_include_append_only_chain_fields(self) -> None:
        for field in (
            "revision_id",
            "record_type",
            "operation",
            "resulting_status",
            "target_revision_id",
            "supersedes_revision_id",
            "git_base_sha",
            "git_commit_sha",
            "receipt_for_revision_id",
        ):
            assert field in LEAN_REVISION_HEADERS


class TestInitializeOnEmptyWorkbook:
    def test_creates_all_seven_tabs_with_exact_expected_headers(self) -> None:
        spreadsheet = FakeSpreadsheet()
        store = SheetsStore(spreadsheet)

        created, removed = store.initialize(
            replace_tabs=False,
            backup_path=None,
            remove_legacy_nfl_tabs=False,
        )

        assert created == 7
        assert removed == 0
        for tab_name, expected_headers in TAB_HEADERS.items():
            worksheet = spreadsheet.worksheet(tab_name)
            assert worksheet.get_all_values()[0] == expected_headers

    def test_populates_default_settings(self) -> None:
        spreadsheet = FakeSpreadsheet()
        store = SheetsStore(spreadsheet)

        store.initialize(
            replace_tabs=False, backup_path=None, remove_legacy_nfl_tabs=False
        )

        settings_rows = spreadsheet.worksheet("wnba_settings").get_all_values()[1:]
        keys = {row[0] for row in settings_rows}
        assert "schema_version" in keys
        assert "schedule_horizon_days" in keys

    def test_seeds_allowed_user_when_requested(self) -> None:
        spreadsheet = FakeSpreadsheet()
        store = SheetsStore(spreadsheet)

        store.initialize(
            replace_tabs=False,
            backup_path=None,
            remove_legacy_nfl_tabs=False,
            seed_allowed_user_id=555,
            seed_allowed_username="owner",
            seed_allowed_display_name="Owner",
        )

        assert store.allowed_user_ids() == {555}

    def test_does_not_require_backup_path_when_non_destructive(self) -> None:
        spreadsheet = FakeSpreadsheet()
        store = SheetsStore(spreadsheet)
        # Should not raise even though backup_path is None.
        store.initialize(
            replace_tabs=False, backup_path=None, remove_legacy_nfl_tabs=False
        )


class TestInitializeIsNonDestructiveByDefault:
    def test_does_not_touch_or_recreate_existing_correctly_headed_tabs(self) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        existing_ids = {ws.title: ws.id for ws in spreadsheet.worksheets()}
        # Put a real data row in wnba_games to prove it survives.
        spreadsheet.worksheet("wnba_games").append_rows(
            [["evt-1"] + [""] * (len(GAME_HEADERS) - 1)],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        store = SheetsStore(spreadsheet)

        created, removed = store.initialize(
            replace_tabs=False, backup_path=None, remove_legacy_nfl_tabs=False
        )

        assert created == 0
        assert removed == 0
        for title, worksheet_id in existing_ids.items():
            assert spreadsheet.worksheet(title).id == worksheet_id
        games_rows = spreadsheet.worksheet("wnba_games").get_all_values()
        assert games_rows[1][0] == "evt-1"

    def test_raises_on_schema_drift_instead_of_silently_overwriting(self) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        # Corrupt one tab's header row.
        drifted = spreadsheet.worksheet("wnba_games")
        drifted._grid[0] = ["not", "the", "expected", "headers"]
        store = SheetsStore(spreadsheet)

        with pytest.raises(ValueError, match="headers do not match"):
            store.initialize(
                replace_tabs=False,
                backup_path=None,
                remove_legacy_nfl_tabs=False,
            )

    def test_replace_tabs_requires_backup_path(self) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        store = SheetsStore(spreadsheet)

        with pytest.raises(ValueError, match="backup path is required"):
            store.initialize(
                replace_tabs=True, backup_path=None, remove_legacy_nfl_tabs=False
            )

    def test_remove_legacy_nfl_tabs_requires_backup_path(self) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        store = SheetsStore(spreadsheet)

        with pytest.raises(ValueError, match="backup path is required"):
            store.initialize(
                replace_tabs=False,
                backup_path=None,
                remove_legacy_nfl_tabs=True,
            )


class TestInitializeDestructivePaths:
    def test_replace_tabs_backs_up_then_recreates_with_clean_headers(
        self, tmp_path: Path
    ) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        drifted = spreadsheet.worksheet("wnba_games")
        drifted._grid[0] = ["stale", "headers"]
        old_id = drifted.id
        store = SheetsStore(spreadsheet)
        backup_path = tmp_path / "backup.json"

        created, removed = store.initialize(
            replace_tabs=True,
            backup_path=backup_path,
            remove_legacy_nfl_tabs=False,
        )

        assert backup_path.exists()
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        assert any(
            tab["title"] == "wnba_games" and tab["values"][0] == ["stale", "headers"]
            for tab in backup["worksheets"]
        )
        assert created == 7
        assert removed == 7
        new_games = spreadsheet.worksheet("wnba_games")
        assert new_games.id != old_id
        assert new_games.get_all_values()[0] == GAME_HEADERS

    def test_remove_legacy_nfl_tabs_deletes_only_the_legacy_tabs(
        self, tmp_path: Path
    ) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        for index, title in enumerate(LEGACY_NFL_TABS):
            spreadsheet._worksheets[title] = FakeWorksheet(
                title, worksheet_id=900 + index, headers=["legacy"]
            )
        store = SheetsStore(spreadsheet)
        backup_path = tmp_path / "backup.json"

        created, removed = store.initialize(
            replace_tabs=False,
            backup_path=backup_path,
            remove_legacy_nfl_tabs=True,
        )

        assert created == 0
        assert removed == len(LEGACY_NFL_TABS)
        remaining_titles = {ws.title for ws in spreadsheet.worksheets()}
        for title in LEGACY_NFL_TABS:
            assert title not in remaining_titles
        assert "wnba_games" in remaining_titles


class TestBackupWorkbook:
    def test_refuses_to_overwrite_an_existing_backup(self, tmp_path: Path) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        store = SheetsStore(spreadsheet)
        backup_path = tmp_path / "backup.json"
        backup_path.write_text("existing", encoding="utf-8")

        with pytest.raises(FileExistsError):
            store.backup_workbook(backup_path)

    def test_captures_every_worksheet_title_and_values(self, tmp_path: Path) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        spreadsheet.worksheet("wnba_thoughts").append_rows(
            [["t1"] + [""] * (len(THOUGHT_HEADERS) - 1)],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        store = SheetsStore(spreadsheet)
        backup_path = tmp_path / "nested" / "backup.json"

        store.backup_workbook(backup_path)

        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        titles = {tab["title"] for tab in backup["worksheets"]}
        assert titles == set(TAB_HEADERS)
        thoughts_tab = next(
            tab for tab in backup["worksheets"] if tab["title"] == "wnba_thoughts"
        )
        assert thoughts_tab["values"][1][0] == "t1"


class TestAllowlistRejection:
    def test_disabled_and_non_numeric_and_non_positive_entries_are_excluded(
        self,
    ) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        spreadsheet.worksheet("wnba_allowed_users").append_rows(
            [
                ["555", "owner", "Owner", "true", "2026-08-01T00:00:00Z", ""],
                ["777", "disabled_user", "Disabled", "false", "2026-08-01T00:00:00Z", ""],
                ["not-a-number", "bad", "Bad", "true", "2026-08-01T00:00:00Z", ""],
                ["-1", "negative", "Negative", "true", "2026-08-01T00:00:00Z", ""],
                ["0", "zero", "Zero", "true", "2026-08-01T00:00:00Z", ""],
            ],
            value_input_option=RAW_VALUE_INPUT_OPTION,
        )
        store = SheetsStore(spreadsheet)

        allowed = store.allowed_user_ids()

        assert allowed == {555}
        assert 777 not in allowed  # disabled
        assert -1 not in allowed  # non-positive
        assert 0 not in allowed  # non-positive

    def test_empty_allowlist_rejects_every_user(self) -> None:
        spreadsheet = _fully_seeded_spreadsheet()
        store = SheetsStore(spreadsheet)

        assert store.allowed_user_ids() == set()

    def test_non_seeded_id_is_absent_after_seeding_a_different_user(self) -> None:
        spreadsheet = FakeSpreadsheet()
        store = SheetsStore(spreadsheet)

        store.initialize(
            replace_tabs=False,
            backup_path=None,
            remove_legacy_nfl_tabs=False,
            seed_allowed_user_id=555,
            seed_allowed_username="owner",
            seed_allowed_display_name="Owner",
        )

        allowed = store.allowed_user_ids()
        assert 555 in allowed
        assert 999999 not in allowed
