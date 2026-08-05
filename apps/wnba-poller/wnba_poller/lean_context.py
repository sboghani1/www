from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol

from .models import parse_timestamp
from .path210_ops import get_event_block


class LeanContextStore(Protocol):
    def read_games(self) -> list[dict[str, Any]]: ...

    def read_snapshots_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def read_thoughts_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def read_game_history(self, *, event_id: str) -> dict[str, Any]: ...


def resolve_current_game(
    store: LeanContextStore,
    *,
    event_id: str,
    expected_matchup: str,
    now: datetime,
    horizon_days: int = 14,
) -> dict[str, Any]:
    if not event_id or len(event_id) > 128:
        raise ValueError("event_id is invalid")
    matches = [
        game
        for game in store.read_games()
        if event_id
        in {
            str(game.get("event_id") or ""),
            str(game.get("espn_event_id") or ""),
        }
    ]
    if len(matches) != 1:
        raise ValueError("event_id does not identify one current WNBA game")
    game = matches[0]
    matchup = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
    if expected_matchup.strip() != matchup:
        raise ValueError("template matchup does not match current Sheet state")
    commence = parse_timestamp(str(game.get("commence_time_utc") or ""))
    if commence < now - timedelta(hours=12):
        raise ValueError("selected WNBA game is no longer current")
    if commence > now + timedelta(days=horizon_days):
        raise ValueError("selected WNBA game is outside the active horizon")
    return game


def extract_path210_context(
    document: str,
    *,
    event_id: str,
    game: Mapping[str, Any],
    max_chars: int = 40000,
) -> dict[str, str]:
    if max_chars < 4000 or max_chars > 100000:
        raise ValueError("path210 context limit is invalid")
    notes_end = document.find("\n# Past Events")
    notes = document[:notes_end] if notes_end >= 0 else document[:12000]

    cache_start = document.find("# Model Cache")
    upcoming_start = document.find("# Upcoming Events")
    cache = ""
    if cache_start >= 0:
        cache_end = upcoming_start if upcoming_start > cache_start else None
        cache = document[cache_start:cache_end]

    matchup = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
    matchup_position = document.find(matchup)
    nearby = ""
    if matchup_position >= 0:
        start = max(upcoming_start, matchup_position - 1000)
        end = min(len(document), matchup_position + 10000)
        nearby = document[start:end]

    current_block = get_event_block(document, event_id) or ""
    sections = {
        "rules": notes,
        "model_cache": cache,
        "selected_game_path_context": nearby,
        "current_event_block": current_block,
    }
    total = sum(len(value) for value in sections.values())
    if total <= max_chars:
        return sections
    remaining = max_chars
    bounded: dict[str, str] = {}
    for key in (
        "rules",
        "model_cache",
        "current_event_block",
        "selected_game_path_context",
    ):
        value = sections[key]
        share = min(len(value), remaining)
        bounded[key] = value[:share]
        remaining -= share
    return bounded


def build_lean_context(
    store: LeanContextStore,
    *,
    event_id: str,
    expected_matchup: str,
    path210_document: str,
    now: datetime,
) -> dict[str, Any]:
    game = resolve_current_game(
        store,
        event_id=event_id,
        expected_matchup=expected_matchup,
        now=now,
    )
    snapshots = store.read_snapshots_for_event(
        event_id=str(game["event_id"])
    )
    snapshot_ids = [
        (
            f"{game['event_id']}:"
            f"{snapshot.get('captured_at_utc', '')}:"
            f"{snapshot.get('bookmaker', '')}"
        )
        for snapshot in snapshots
    ]
    return {
        "game": game,
        "snapshots": snapshots,
        "snapshot_ids": snapshot_ids,
        "thoughts": store.read_thoughts_for_event(
            event_id=str(game["event_id"])
        ),
        "lean_history": store.read_game_history(
            event_id=str(game["event_id"])
        ),
        "path210": extract_path210_context(
            path210_document,
            event_id=str(game["event_id"]),
            game=game,
        ),
    }
