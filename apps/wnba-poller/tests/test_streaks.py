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
        streaks = compute_streaks(results)
        assert streaks["A"]["streak"] == "W2"
        assert streaks["A"]["wins"] == 3
        assert streaks["A"]["losses"] == 1

    def test_orders_by_date_regardless_of_input_order(self) -> None:
        results = [
            _game("2026-05-14", "A", "E", "E"),  # latest: A loses
            _game("2026-05-08", "A", "B", "A"),
            _game("2026-05-10", "A", "C", "A"),
        ]
        # Chronologically A won, won, lost -> current streak L1.
        assert compute_streaks(results)["A"]["streak"] == "L1"

    def test_ignores_rows_with_a_winner_not_in_the_matchup(self) -> None:
        results = [_game("2026-05-08", "A", "B", "Z")]
        assert compute_streaks(results) == {}


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
        assert out["streaks"]["A"]["streak"] == "W1"
        assert store.settings["streak_cache_completed_games"] == "1"
        assert json.loads(store.settings["streak_cache"])["A"]["streak"] == "W1"

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
        assert out["streaks"]["A"]["streak"] == "L1"
        assert store.update_calls == 2

    def test_ignores_a_corrupt_cache_value(self) -> None:
        store = _FakeStore([_game("2026-05-08", "A", "B", "A")])
        store.settings = {
            "streak_cache_completed_games": "1",
            "streak_cache": "not json",
        }
        out = get_streaks(store, now=_now())
        assert out["cached"] is False
        assert out["streaks"]["A"]["streak"] == "W1"
