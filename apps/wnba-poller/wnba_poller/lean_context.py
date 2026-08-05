from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol

from .models import parse_timestamp
from .path210_ops import get_event_block

_ENTRY_SPLIT_RE = re.compile(r"\n\s*\n")


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


def _section_mascot(team_name: str) -> str:
    parts = str(team_name).strip().split()
    return parts[-1] if parts else ""


def _split_entries(section: str) -> list[str]:
    return [
        chunk.strip()
        for chunk in _ENTRY_SPLIT_RE.split(section)
        if chunk.strip()
    ]


def _find_precedent_entries(
    past_events_section: str,
    *,
    away_team: str,
    home_team: str,
    max_entries: int,
) -> list[str]:
    away_mascot = _section_mascot(away_team).lower()
    home_mascot = _section_mascot(home_team).lower()
    if not away_mascot and not home_mascot:
        return []
    matches = []
    for entry in _split_entries(past_events_section):
        lowered = entry.lower()
        if (away_mascot and away_mascot in lowered) or (
            home_mascot and home_mascot in lowered
        ):
            matches.append(entry)
    return matches[-max_entries:]


def extract_path210_context(
    document: str,
    *,
    event_id: str,
    game: Mapping[str, Any],
    max_chars: int = 40000,
    max_precedent_entries: int = 8,
) -> dict[str, str]:
    if max_chars < 4000 or max_chars > 100000:
        raise ValueError("path210 context limit is invalid")

    # Headings must be found anchored to a line start (`\n# ...`). The
    # unanchored form used to match this same text quoted inline inside the
    # "Notes For Model" rules prose itself (e.g. "the '# Model Cache'
    # section"), which sits before every real heading -- so cache/upcoming
    # extraction silently sliced from the wrong, much-earlier position.
    past_events_start = document.find("\n# Past Events")
    cache_start = document.find("\n# Model Cache")
    upcoming_start = document.find("\n# Upcoming Events")

    notes = (
        document[:past_events_start]
        if past_events_start >= 0
        else document[:12000]
    )

    cache = ""
    if cache_start >= 0:
        cache_end = upcoming_start if upcoming_start > cache_start else None
        cache = document[cache_start:cache_end]

    away_team = str(game.get("away_team", ""))
    home_team = str(game.get("home_team", ""))
    matchup = f"{away_team} @ {home_team}"

    # Precedent: search the (correctly bounded) Past Events section for
    # entries mentioning either team's mascot -- entries reference teams by
    # mascot name in prose (e.g. "the aces moved from -1 to +3") and by
    # entry-name prefix (e.g. "95fadevalkyries"), never by the literal
    # "Away @ Home" string, so a substring search for the matchup itself
    # never found anything here.
    precedent = ""
    if past_events_start >= 0:
        if cache_start > past_events_start:
            past_events_end = cache_start
        elif upcoming_start > past_events_start:
            past_events_end = upcoming_start
        else:
            past_events_end = len(document)
        past_events_section = document[past_events_start:past_events_end]
        precedent = "\n\n".join(
            _find_precedent_entries(
                past_events_section,
                away_team=away_team,
                home_team=home_team,
                max_entries=max_precedent_entries,
            )
        )

    # Continuation: an existing "# Upcoming Events" entry for this exact
    # future matchup (see path210's own '<name>_cont' convention). Bounded
    # to the Upcoming Events section itself so a match can never resolve to
    # an out-of-order/empty slice.
    nearby = ""
    if upcoming_start >= 0:
        upcoming_section = document[upcoming_start:]
        matchup_position = upcoming_section.find(matchup)
        if matchup_position >= 0:
            start = max(0, matchup_position - 1000)
            end = min(len(upcoming_section), matchup_position + 10000)
            nearby = upcoming_section[start:end]

    selected_game_path_context = "\n\n".join(
        part for part in (precedent, nearby) if part
    )

    current_block = get_event_block(document, event_id) or ""
    sections = {
        "rules": notes,
        "model_cache": cache,
        "selected_game_path_context": selected_game_path_context,
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
