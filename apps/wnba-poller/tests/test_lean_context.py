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

    def test_allow_started_permits_a_long_finished_game(self) -> None:
        # Resolution deliberately opts out of the "no longer current" cutoff
        # -- it only ever operates on games that have already finished,
        # often the next day.
        store = FakeStore(
            games=[{**GAME, "commence_time_utc": "2026-08-01T00:00:00Z"}]
        )
        game = resolve_current_game(
            store,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            now=_now(),
            allow_started=True,
        )
        assert game["event_id"] == "evt-1"

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


def _real_shaped_document(current_block: str = "") -> str:
    # Mirrors the real apps/wnba-poller/path210.md structure and ordering:
    # Notes For Model -> Past Events -> Model Cache -> Upcoming Events.
    # Critically, the Notes prose quotes these same heading strings inline
    # (e.g. "the '# Model Cache' section") *before* any real heading, which
    # is exactly what broke unanchored substring search in production.
    return (
        "# Notes For Model\n\n"
        "Some curated intro rules.\n\n"
        "1. Rebuild the '# Model Cache' section after any change, and read "
        "'# Upcoming Events' before betting. Also see '# Past Events' for "
        "history.\n"
        "\n# Past Events\n\n"
        "1fadesparks\n"
        "wrong\n"
        "some_tag\n"
        "context: the sparks lost badly as home favorites.\n\n"
        "2fadeaces\n"
        "right\n"
        "another_tag\n"
        "context: the aces covered easily as home favorites.\n\n"
        "3fadefever\n"
        "wrong\n"
        "third_tag\n"
        "context: the fever blew a big lead late.\n\n"
        "4fadesky\n"
        "right\n"
        "fourth_tag\n"
        "context: the sky won outright as home dogs.\n\n"
        "5fadeaces\n"
        "right\n"
        "fifth_tag\n"
        "context: the aces closed as road favorites and covered.\n"
        "\n# Model Cache\n\n"
        "cache contents here\n"
        "\n# Upcoming Events\n\n"
        "Indiana Fever @ Las Vegas Aces preview text.\n\n"
        f"{current_block}\n"
    )


class TestExtractPath210Context:
    def test_extracts_rules_and_model_cache_with_real_document_order(
        self,
    ) -> None:
        document = _real_shaped_document()
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        assert "Some curated intro rules." in sections["rules"]
        assert "1fadesparks" not in sections["rules"]
        assert "# Model Cache" in sections["model_cache"]
        assert "cache contents here" in sections["model_cache"]

    def test_unanchored_inline_heading_mentions_do_not_confuse_extraction(
        self,
    ) -> None:
        # Regression test for the exact production bug: the Notes prose
        # quotes "# Model Cache" and "# Upcoming Events" inline, before the
        # real headings. An unanchored `document.find(...)` would match
        # those and (since the inline "# Upcoming Events" mention sits
        # *before* the inline "# Model Cache" mention) fall into the
        # `cache_end = None` branch, slicing model_cache all the way to the
        # end of the document -- including every Past Events entry.
        document = _real_shaped_document()
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        assert "1fadesparks" not in sections["model_cache"]
        assert "fadeaces" not in sections["model_cache"]
        assert len(sections["model_cache"]) < 200

    def test_precedent_entries_are_selected_by_team_mascot(self) -> None:
        document = _real_shaped_document()
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        context = sections["selected_game_path_context"]
        assert "2fadeaces" in context
        assert "3fadefever" in context
        assert "5fadeaces" in context
        # Neither mascot (Aces/Fever) is mentioned in these entries.
        assert "1fadesparks" not in context
        assert "4fadesky" not in context

    def test_precedent_caps_to_max_entries_preferring_recent(self) -> None:
        document = _real_shaped_document()
        sections = extract_path210_context(
            document,
            event_id="evt-1",
            game=GAME,
            max_precedent_entries=1,
        )
        context = sections["selected_game_path_context"]
        # Three entries mention Aces/Fever (2fadeaces, 3fadefever,
        # 5fadeaces); capped to 1 keeps only the most recent.
        assert "5fadeaces" in context
        assert "2fadeaces" not in context
        assert "3fadefever" not in context

    def test_upcoming_events_continuation_is_combined_with_precedent(
        self,
    ) -> None:
        document = _real_shaped_document()
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        context = sections["selected_game_path_context"]
        assert "Indiana Fever @ Las Vegas Aces preview text." in context
        assert "5fadeaces" in context

    def test_current_event_block_is_extracted_independently(self) -> None:
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
        document = _real_shaped_document(current_block=block)
        sections = extract_path210_context(
            document, event_id="evt-1", game=GAME
        )
        assert sections["current_event_block"] == block

    def test_no_precedent_when_no_team_mentioned(self) -> None:
        document = _real_shaped_document().replace(
            "Indiana Fever @ Las Vegas Aces preview text.\n\n", ""
        )
        other_game = {**GAME, "away_team": "Chicago Sky", "home_team": "Dallas Wings"}
        sections = extract_path210_context(
            document, event_id="evt-1", game=other_game
        )
        # Sky/Wings: "4fadesky" mentions the Sky mascot, should be picked up.
        assert "4fadesky" in sections["selected_game_path_context"]
        assert "2fadeaces" not in sections["selected_game_path_context"]

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
