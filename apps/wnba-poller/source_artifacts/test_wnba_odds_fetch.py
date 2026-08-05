#!/usr/bin/env python3
"""Unit tests for WNBA odds log deduplication.

Usage:
    .venv/bin/python -m unittest test_wnba_odds_fetch.py
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wnba_odds_fetch import append_new_lines, matchup_csv, slate_csv


def _line(minutes_to_tip, dream_spread="-7.5", total="182.5"):
    return (
        f"Atlanta Dream @ Toronto Tempo | T-{minutes_to_tip}min "
        f"({minutes_to_tip / 60:.1f}h) | SPREADS: "
        f"Atlanta Dream {dream_spread} (-110), "
        f"Toronto Tempo {float(dream_spread) * -1:g} (-110) | TOTALS: "
        f"Over {total} (-110), Under {total} (-110)"
    )


class AppendNewLinesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "wnba_lines_log.txt"
        self.log_file.write_text(_line(180) + "\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_skips_unchanged_odds_before_one_hour(self):
        appended = append_new_lines([_line(121)], self.log_file)

        self.assertEqual(appended, 0)
        self.assertEqual(len(self.log_file.read_text().splitlines()), 1)

    def test_appends_unchanged_odds_after_one_hour(self):
        appended = append_new_lines([_line(120)], self.log_file)

        self.assertEqual(appended, 1)
        self.assertEqual(len(self.log_file.read_text().splitlines()), 2)

    def test_appends_changed_odds_immediately(self):
        appended = append_new_lines([_line(165, dream_spread="-8.0")], self.log_file)

        self.assertEqual(appended, 1)
        self.assertEqual(len(self.log_file.read_text().splitlines()), 2)

    def test_hourly_timer_resets_after_snapshot(self):
        self.assertEqual(append_new_lines([_line(120)], self.log_file), 1)
        self.assertEqual(append_new_lines([_line(75)], self.log_file), 0)
        self.assertEqual(len(self.log_file.read_text().splitlines()), 2)

    def test_csv_keeps_hourly_duplicate_snapshots(self):
        self.log_file.write_text(_line(180) + "\n" + _line(120) + "\n")

        rows = matchup_csv("Dream", self.log_file).splitlines()

        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[1].startswith("1,3.0,"))
        self.assertTrue(rows[2].startswith("2,2.0,"))

    def test_csv_skips_snapshots_without_markets(self):
        empty_line = (
            "Atlanta Dream @ Toronto Tempo | T-240min (4.0h) "
            "| SPREADS:  | TOTALS: "
        )
        self.log_file.write_text(empty_line + "\n" + _line(180) + "\n")

        rows = matchup_csv("Dream", self.log_file).splitlines()

        self.assertEqual(len(rows), 2)
        self.assertIn("atlanta_dream_spread", rows[0])
        self.assertTrue(rows[1].startswith("1,3.0,"))

    def test_csv_selects_current_repeated_fixture(self):
        old_game = [
            _line(2520, dream_spread="-1.5"),
            _line(2460, dream_spread="-1.5"),
            _line(1186, dream_spread="-1.0"),
        ]
        current_game = [
            _line(2193, dream_spread="1.0"),
            _line(2133, dream_spread="1.0"),
            _line(1172, dream_spread="3.5"),
            _line(90, dream_spread="4.0"),
        ]
        self.log_file.write_text("\n".join(old_game + current_game) + "\n")

        rows = matchup_csv(
            "Atlanta Dream @ Toronto Tempo",
            self.log_file,
            target_minutes=90,
        ).splitlines()

        self.assertEqual(len(rows), 5)
        self.assertTrue(rows[1].startswith("1,36.5,"))
        self.assertTrue(rows[-1].startswith("4,1.5,"))
        self.assertFalse(any(",42.0," in row for row in rows))

    def test_csv_separates_overlapping_repeated_fixtures(self):
        entries = [
            _line(300, dream_spread="-7.5"),
            _line(3000, dream_spread="-9.5"),
            _line(240, dream_spread="-7.5"),
            _line(2940, dream_spread="-9.5"),
            _line(180, dream_spread="-7.5"),
            _line(2880, dream_spread="-9.5"),
        ]
        self.log_file.write_text("\n".join(entries) + "\n")

        rows = matchup_csv(
            "Atlanta Dream @ Toronto Tempo",
            self.log_file,
            target_minutes=180,
        ).splitlines()

        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[1].startswith("1,5.0,"))
        self.assertTrue(rows[2].startswith("2,4.0,"))
        self.assertTrue(rows[3].startswith("3,3.0,"))

    def test_slate_csv_includes_latest_unlogged_snapshot(self):
        latest_file = Path(self.temp_dir.name) / "wnba_lines.txt"
        self.log_file.write_text(_line(120) + "\n")
        latest_file.write_text(_line(90) + "\n")

        export = slate_csv(
            "2026-07-20",
            latest_file,
            self.log_file,
            snapshot_time=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        )
        rows = export.splitlines()

        self.assertEqual(rows[0], "Atlanta Dream @ Toronto Tempo")
        self.assertTrue(rows[-2].startswith("1,2.0,"))
        self.assertTrue(rows[-1].startswith("2,1.5,"))

    def test_slate_csv_retains_started_repeated_fixture(self):
        latest_file = Path(self.temp_dir.name) / "wnba_lines.txt"
        previous_export = Path(self.temp_dir.name) / "wnba_lines_2026-07-20.txt"
        old_game = [
            _line(2310, dream_spread="-1.5"),
            _line(2250, dream_spread="-1.5"),
            _line(6, dream_spread="-1.0"),
        ]
        current_game = [
            _line(3324, dream_spread="1.0"),
            _line(3264, dream_spread="1.0"),
            _line(149, dream_spread="3.5"),
        ]
        self.log_file.write_text("\n".join(old_game + current_game) + "\n")
        previous_csv = matchup_csv(
            "Atlanta Dream @ Toronto Tempo",
            self.log_file,
            target_minutes=149,
        )
        previous_export.write_text(
            "Atlanta Dream @ Toronto Tempo\n" + previous_csv + "\n"
        )
        with self.log_file.open("a") as f:
            f.write(_line(18, dream_spread="4.0") + "\n")
        latest_file.write_text("")

        export = slate_csv(
            "2026-07-20",
            latest_file,
            self.log_file,
            snapshot_time=datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc),
            previous_export_file=previous_export,
        )
        rows = export.splitlines()

        self.assertEqual(rows[0], "Atlanta Dream @ Toronto Tempo")
        self.assertTrue(rows[2].startswith("1,55.4,"))
        self.assertTrue(rows[-1].startswith("4,0.3,"))
        self.assertFalse(any(",38.5," in row for row in rows))


if __name__ == "__main__":
    unittest.main()
