"""Per-team win/loss streaks for the WNBA season.

Streaks are computed lazily from the ``wnba_results`` log and cached in
``wnba_settings`` with the completed-game count they were computed for. When
streaks are requested, that count is the only staleness signal: if it is
unchanged, the cache is returned as-is; otherwise streaks are recomputed and
the cache is re-stamped. No separate process keeps the cache accurate -- a new
completed game is the sole trigger for a recompute.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Protocol


class _StreakStore(Protocol):
    def read_results(self) -> list[dict[str, Any]]: ...

    def read_settings(self) -> dict[str, str]: ...

    def update_settings(
        self, updates: dict[str, Any], *, now: datetime
    ) -> None: ...


_CACHE_COUNT_KEY = "streak_cache_completed_games"
_CACHE_VALUE_KEY = "streak_cache"


def compute_streaks(
    results: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return ``{team: {"streak": "W3", "wins": n, "losses": m}}``.

    ``results`` are completed games (each with ``game_date_et``,
    ``away_team``, ``home_team``, ``winner``). Games are ordered by ET date so
    the current streak is the trailing run of identical outcomes; a team plays
    at most once per date, so date order is a stable chronological order.
    """
    ordered = sorted(results, key=lambda r: str(r.get("game_date_et", "")))
    sequences: dict[str, list[bool]] = {}
    for game in ordered:
        away = str(game.get("away_team", "")).strip()
        home = str(game.get("home_team", "")).strip()
        winner = str(game.get("winner", "")).strip()
        if not away or not home or winner not in {away, home}:
            continue
        sequences.setdefault(away, []).append(winner == away)
        sequences.setdefault(home, []).append(winner == home)

    streaks: dict[str, dict[str, Any]] = {}
    for team, outcomes in sequences.items():
        wins = sum(1 for won in outcomes if won)
        losses = len(outcomes) - wins
        last = outcomes[-1]
        run = 0
        for won in reversed(outcomes):
            if won == last:
                run += 1
            else:
                break
        streaks[team] = {
            "streak": f"{'W' if last else 'L'}{run}",
            "wins": wins,
            "losses": losses,
        }
    return streaks


def get_streaks(
    store: _StreakStore, *, now: datetime
) -> dict[str, Any]:
    """Return cached streaks, recomputing only when a game has finished.

    The result carries ``completed_games`` and ``cached`` (True when served
    from the cache without recomputation).
    """
    results = store.read_results()
    completed = len(results)
    settings = store.read_settings()

    cached_count = settings.get(_CACHE_COUNT_KEY, "")
    cached_value = settings.get(_CACHE_VALUE_KEY, "")
    if cached_value and cached_count == str(completed):
        try:
            streaks = json.loads(cached_value)
        except (ValueError, TypeError):
            streaks = None
        if isinstance(streaks, dict):
            return {
                "completed_games": completed,
                "cached": True,
                "streaks": streaks,
            }

    streaks = compute_streaks(results)
    store.update_settings(
        {
            _CACHE_COUNT_KEY: completed,
            _CACHE_VALUE_KEY: json.dumps(streaks, ensure_ascii=False),
        },
        now=now,
    )
    return {
        "completed_games": completed,
        "cached": False,
        "streaks": streaks,
    }
