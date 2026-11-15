import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import track_live as t  # noqa: E402


def game(state="in", name="STATUS_IN_PROGRESS", detail="", period=1, clock="5:00"):
    return {
        "espn_id": "1", "tip_utc": "2026-11-15T00:00Z", "short": "A @ B",
        "state": state, "status_name": name, "detail": detail,
        "period": period, "clock": clock,
        "home": {"nickname": "Cavaliers"}, "away": {"nickname": "Mavericks"},
    }


END_Q1 = dict(name="STATUS_END_PERIOD", detail="End of 1st Quarter", period=1, clock="0.0")
HALF = dict(name="STATUS_HALFTIME", detail="Halftime", period=2, clock="0.0")


class BreakDetection(unittest.TestCase):
    def test_breaks(self):
        self.assertEqual(t.break_kind(game(**END_Q1)), "q1")
        self.assertEqual(t.break_kind(game(**HALF)), "half")
        self.assertIsNone(t.break_kind(game(period=1, clock="1:23")))
        self.assertEqual(t.missed_break(game(period=2, clock="11:30")), "q1")
        self.assertEqual(t.missed_break(game(period=3, clock="11:40")), "half")
        self.assertIsNone(t.missed_break(game(period=3, clock="9:00")))


class Captures(unittest.TestCase):
    def test_two_captures_per_break_spaced(self):
        g = game(**HALF)
        self.assertEqual(t.due_capture(g, {}, 1000), ("half", False))
        self.assertIsNone(t.due_capture(g, {"half": [1000]}, 1100))
        self.assertEqual(t.due_capture(g, {"half": [1000]}, 1300), ("half", False))
        self.assertIsNone(t.due_capture(g, {"half": [1000, 1300]}, 2000))

    def test_late_capture_only_if_never_captured(self):
        g = game(period=2, clock="11:45")
        self.assertEqual(t.due_capture(g, {}, 0), ("q1", True))
        self.assertIsNone(t.due_capture(g, {"q1": [1]}, 0))

    def test_second_halftime_capture_is_not_skipped(self):
        # The 2026-11-14 bug: the run ended right after the last game's first
        # halftime capture.
        g = game(**HALF)
        self.assertTrue(t.still_pending(g, {"q1": [1, 2], "half": [3]}))
        self.assertFalse(t.still_pending(g, {"q1": [1, 2], "half": [3, 4]}))
        self.assertFalse(t.still_pending(game(period=3, clock="5:00"), {"half": [3]}))
        self.assertTrue(t.still_pending(game(period=3, clock="11:50"), {}))
        self.assertFalse(t.still_pending(game(state="post"), {}))

    def test_interval_waits_for_second_halftime_capture(self):
        g = game(**HALF)
        state = {"captures": {"1": {"half": [1000]}}, "pregame": {}}
        self.assertAlmostEqual(t.interval([g], state, 1100), 200)


class Markets(unittest.TestCase):
    EVENT = {
        "description": "Dallas Mavericks @ Cleveland Cavaliers",
        "displayGroups": [{"description": "Game Lines", "markets": [
            {"description": "Point Spread", "period": {"abbreviation": "G"}, "status": "O",
             "outcomes": [{"description": "Dallas Mavericks", "type": "A", "status": "O",
                           "price": {"american": "-115", "handicap": "5.5"}},
                          {"description": "Cleveland Cavaliers", "type": "H", "status": "O",
                           "price": {"american": "-115", "handicap": "-5.5"}}]},
            {"description": "Moneyline", "period": {"abbreviation": "G"}, "status": "O",
             "outcomes": [{"description": "Dallas Mavericks", "price": {"american": "+200"}},
                          {"description": "Cleveland Cavaliers", "price": {"american": "-275"}}]},
            {"description": "Player Points", "period": {"abbreviation": "G"}, "outcomes": []},
        ]}],
    }

    def test_parse_and_match(self):
        mk = t.parse_markets(self.EVENT)
        self.assertEqual([m["market"] for m in mk], ["point spread", "moneyline"])
        self.assertEqual(t.full_game_line(mk), (("5.5", "-5.5"), ("+200", "-275")))
        self.assertIs(t.match_event(game(), [self.EVENT]), self.EVENT)
        self.assertIsNone(t.match_event(game(), []))


if __name__ == "__main__":
    unittest.main()
