from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from wnba_poller.lean_context import (
    build_lean_context,
    extract_path210_context,
    resolve_current_game,
)
from wnba_poller.path210_ops import render_event_block

GAME = {
    "event_id": "evt-1",
    "espn_event_id": "espn-1",
    "commence_time_utc": "2026-08-10T23:00:00Z",
    "commence_time_et": "2026-08-10T19:00:00-04:00",
    "away_team": "Indiana Fever",
    "home_team": "Las Vegas Aces",
}


def _now() -> datetime:
    return datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, games: list[dict[str, Any]] | None = None) -> None:
        self.games = games if games is not None else [GAME]

    def read_games(self) -> list[dict[str, Any]]:
        return self.games

    def read_snapshots_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [
            {"captured_at_utc": "2026-08-09T12:00:00Z", "bookmaker": "betonlineag"}
        ]

    def read_thoughts_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [{"thought_id": "t1", "thought_text": "Watch the Aces bench."}]

    def read_game_history(self, *, event_id: str) -> dict[str, Any]:
        return {"active_revision": None, "revision_history": []}


class TestResolveCurrentGame:
    def test_resolves_by_event_id(self) -> None:
        game = resolve_current_game(
            FakeStore(),
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            now=_now(),
        )
        assert game["event_id"] == "evt-1"

    def test_resolves_by_espn_event_id(self) -> None:
        game = resolve_current_game(
            FakeStore(),
            event_id="espn-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            now=_now(),
        )
        assert game["espn_event_id"] == "espn-1"

    def test_rejects_empty_event_id(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            resolve_current_game(
                FakeStore(),
                event_id="",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                now=_now(),
            )

    def test_rejects_ambiguous_event_id(self) -> None:
        store = FakeStore(games=[GAME, {**GAME, "espn_event_id": "evt-1"}])
        with pytest.raises(ValueError, match="one current WNBA game"):
            resolve_current_game(
                store,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                now=_now(),
            )

    def test_rejects_mismatched_matchup(self) -> None:
        with pytest.raises(ValueError, match="matchup"):
            resolve_current_game(
                FakeStore(),
                event_id="evt-1",
                expected_matchup="Wrong Team @ Other Team",
                now=_now(),
            )

    def test_rejects_started_game(self) -> None:
        store = FakeStore(
            games=[{**GAME, "commence_time_utc": "2026-08-01T00:00:00Z"}]
        )
        with pytest.raises(ValueError, match="no longer current"):
            resolve_current_game(
                store,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                now=_now(),
            )

    def test_rejects_game_outside_horizon(self) -> None:
        store = FakeStore(
            games=[{**GAME, "commence_time_utc": "2026-09-15T00:00:00Z"}]
        )
        with pytest.raises(ValueError, match="active horizon"):
            resolve_current_game(
                store,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                now=_now(),
            )


class TestExtractPath210Context:
    def test_extracts_rules_cache_and_event_block(self) -> None:
        output = {
            "full_game": {
                "side": {
                    "selection": "Las Vegas Aces",
                    "strength": "moderate",
                    "evidence": ["x"],
                    "watch_conditions": ["y"],
                },
                "total": {
                    "selection": "Over",
                    "strength": "small",
                    "evidence": ["x"],
                    "watch_conditions": ["y"],
                },
            },
            "first_half": {},
            "summary": "Aces favored.",
        }
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=output
        )
        document = (
            "Curated intro rules.\n\n"
            "# Model Cache\ncache contents here\n\n"
            "# Upcoming Events\n"
            "Indiana Fever @ Las Vegas Aces preview text.\n\n"
            f"{block}\n\n"
            "# Past Events\nhistorical stuff not needed for context\n"
        )
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        assert "Curated intro rules." in sections["rules"]
        assert "historical stuff" not in sections["rules"]
        assert "cache contents here" in sections["model_cache"]
        assert "Indiana Fever @ Las Vegas Aces preview text." in (
            sections["selected_game_path_context"]
        )
        assert sections["current_event_block"] == block

    def test_bounds_total_size_to_max_chars(self) -> None:
        document = "A" * 5000 + "\n# Past Events\n" + "B" * 5000
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME, max_chars=4000
        )
        assert sum(len(value) for value in sections.values()) <= 4000

    def test_rejects_limit_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            extract_path210_context(
                "doc", event_id="evt-1", game=GAME, max_chars=100
            )


class TestBuildLeanContext:
    def test_assembles_full_context(self) -> None:
        store = FakeStore()
        context = build_lean_context(
            store,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            path210_document="# path210\n\nrules\n",
            now=_now(),
        )
        assert context["game"]["event_id"] == "evt-1"
        assert context["snapshot_ids"] == [
            "evt-1:2026-08-09T12:00:00Z:betonlineag"
        ]
        assert context["thoughts"][0]["thought_id"] == "t1"
        assert context["lean_history"]["active_revision"] is None
        assert "path210" in context
