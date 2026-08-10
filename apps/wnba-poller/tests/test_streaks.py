from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from wnba_poller.streaks import compute_streaks, get_streaks


def _game(date: str, away: str, home: str, winner: str) -> dict[str, Any]:
    return {
        "game_date_et": date,
        "away_team": away,
        "home_team": home,
        "winner": winner,
    }


class TestComputeStreaks:
    def test_current_streak_is_the_trailing_run(self) -> None:
        results = [
            _game("2026-05-08", "A", "B", "A"),  # A W, B L
            _game("2026-05-10", "A", "C", "C"),  # A L, C W
            _game("2026-05-12", "D", "A", "A"),  # A W, D L
            _game("2026-05-14", "A", "E", "A"),  # A W (A: W,L,W,W -> W2)
        ]
        teams = compute_streaks(results)["teams"]
        assert teams["A"]["streak"] == "W2"
        assert teams["A"]["wins"] == 3
        assert teams["A"]["losses"] == 1

    def test_longest_win_and_loss_runs(self) -> None:
        # A: W W W L L -> current L2, longest win 3, longest loss 2
        results = [
            _game("2026-05-08", "A", "B", "A"),
            _game("2026-05-09", "A", "C", "A"),
            _game("2026-05-10", "A", "D", "A"),
            _game("2026-05-11", "A", "E", "E"),
            _game("2026-05-12", "A", "F", "F"),
        ]
        a = compute_streaks(results)["teams"]["A"]
        assert a["streak"] == "L2"
        assert a["longest_win"] == 3
        assert a["longest_loss"] == 2

    def test_league_records_pick_the_best_run_and_holders(self) -> None:
        results = [
            _game("2026-05-08", "A", "B", "A"),
            _game("2026-05-09", "A", "C", "A"),  # A win streak 2
            _game("2026-05-10", "D", "B", "D"),  # B loses again
        ]
        league = compute_streaks(results)["league"]
        assert league["longest_win"]["length"] == 2
        assert league["longest_win"]["teams"] == ["A"]
        assert league["longest_loss"]["length"] == 2
        assert league["longest_loss"]["teams"] == ["B"]

    def test_orders_by_date_regardless_of_input_order(self) -> None:
        results = [
            _game("2026-05-14", "A", "E", "E"),  # latest: A loses
            _game("2026-05-08", "A", "B", "A"),
            _game("2026-05-10", "A", "C", "A"),
        ]
        # Chronologically A won, won, lost -> current streak L1.
        assert compute_streaks(results)["teams"]["A"]["streak"] == "L1"

    def test_ignores_rows_with_a_winner_not_in_the_matchup(self) -> None:
        results = [_game("2026-05-08", "A", "B", "Z")]
        assert compute_streaks(results)["teams"] == {}


class _FakeStore:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.settings: dict[str, str] = {}
        self.update_calls = 0

    def read_results(self) -> list[dict[str, Any]]:
        return self._results

    def read_settings(self) -> dict[str, str]:
        return dict(self.settings)

    def update_settings(self, updates: dict[str, Any], *, now: datetime) -> None:
        self.update_calls += 1
        self.settings.update({k: str(v) for k, v in updates.items()})


def _now() -> datetime:
    return datetime(2026, 8, 9, tzinfo=timezone.utc)


class TestGetStreaksCache:
    def test_computes_and_stamps_the_completed_game_count(self) -> None:
        store = _FakeStore([_game("2026-05-08", "A", "B", "A")])
        out = get_streaks(store, now=_now())
        assert out["cached"] is False
        assert out["completed_games"] == 1
        assert out["teams"]["A"]["streak"] == "W1"
        assert out["league"]["longest_win"]["teams"] == ["A"]
        assert store.settings["streak_cache_completed_games"] == "1"
        cached = json.loads(store.settings["streak_cache"])
        assert cached["teams"]["A"]["streak"] == "W1"

    def test_second_call_uses_cache_without_recomputing(self) -> None:
        store = _FakeStore([_game("2026-05-08", "A", "B", "A")])
        get_streaks(store, now=_now())
        assert store.update_calls == 1
        again = get_streaks(store, now=_now())
        assert again["cached"] is True
        assert store.update_calls == 1  # no re-stamp

    def test_recomputes_when_a_new_game_completes(self) -> None:
        results = [_game("2026-05-08", "A", "B", "A")]
        store = _FakeStore(results)
        get_streaks(store, now=_now())
        # A new completed game arrives -> count changes -> recompute.
        results.append(_game("2026-05-10", "A", "C", "C"))
        out = get_streaks(store, now=_now())
        assert out["cached"] is False
        assert out["completed_games"] == 2
        assert out["teams"]["A"]["streak"] == "L1"
        assert store.update_calls == 2

    def test_ignores_a_corrupt_cache_value(self) -> None:
        store = _FakeStore([_game("2026-05-08", "A", "B", "A")])
        store.settings = {
            "streak_cache_completed_games": "1",
            "streak_cache": "not json",
        }
        out = get_streaks(store, now=_now())
        assert out["cached"] is False
        assert out["teams"]["A"]["streak"] == "W1"
