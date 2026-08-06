from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from wnba_poller.lean_revisions import (
    build_publication_receipt,
    build_revision_event,
    derive_revision_history,
)
from wnba_poller.receptionist_helper import handle_request

GAME = {
    "event_id": "evt-1",
    "espn_event_id": "espn-1",
    "status": "final",
    "commence_time_utc": "2026-08-05T23:00:00Z",
    "commence_time_et": "2026-08-05T19:00:00-04:00",
    "away_team": "Phoenix Mercury",
    "home_team": "Atlanta Dream",
    "away_score": "82",
    "home_score": "96",
    "latest_away_spread": "7",
    "latest_home_spread": "-7",
    "latest_total": "181.5",
    "bookmaker": "BetOnline.ag",
}

OUTPUT = {
    "full_game": {
        "side": {
            "selection": "Phoenix Mercury",
            "strength": "watch",
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
    "summary": "Test lean.",
    "source_snapshot_ids": [],
}


def _now() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _active_revision_event(event_id: str, revision_id: str) -> dict[str, Any]:
    return build_revision_event(
        game={**GAME, "event_id": event_id},
        operation="create",
        output=OUTPUT,
        request_text="x",
        source="receptionist",
        now=_now(),
        git_base_sha="a" * 40,
        content_hash="hash",
        revision_id=revision_id,
    )


def _receipt(event_id: str, revision_id: str) -> dict[str, Any]:
    return build_publication_receipt(
        revision_id=revision_id,
        event_id=event_id,
        commit_sha="c" * 40,
        branch="main",
        now=_now(),
    )


class FakeStore:
    def __init__(
        self,
        *,
        games: list[dict[str, Any]] | None = None,
        revision_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.games = games if games is not None else [dict(GAME)]
        self.revision_events = revision_events or []

    def read_games(self) -> list[dict[str, Any]]:
        return self.games

    def read_all_lean_revision_events(self) -> list[dict[str, Any]]:
        return self.revision_events

    def read_game_history(self, *, event_id: str) -> dict[str, Any]:
        return derive_revision_history(
            self.revision_events, event_id=event_id
        )


class TestListResolvableGames:
    def test_lists_only_events_with_an_active_unresolved_lean(self) -> None:
        store = FakeStore(
            games=[
                {**GAME, "event_id": "evt-1"},
                {**GAME, "event_id": "evt-2", "away_team": "Seattle Storm"},
                {**GAME, "event_id": "evt-3", "away_team": "Dallas Wings"},
            ],
            revision_events=[
                # evt-1: has an active lean -- resolvable.
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
                # evt-2: no lean at all -- not resolvable.
                # evt-3: already resolved -- not resolvable again.
                _active_revision_event("evt-3", "rev-3"),
                _receipt("evt-3", "rev-3"),
            ],
        )

        result = handle_request(
            {"action": "list_resolvable_games"}, store=store, now=_now()
        )

        event_ids = {game["event_id"] for game in result["games"]}
        assert event_ids == {"evt-1", "evt-3"}

    def test_excludes_already_resolved_events(self) -> None:
        resolve_record = build_revision_event(
            game={**GAME, "event_id": "evt-1"},
            operation="resolve",
            output=OUTPUT,
            request_text="x",
            source="claude-skill",
            now=_now(),
            git_base_sha="a" * 40,
            content_hash="hash",
            revision_id="rev-2",
            supersedes_revision_id="rev-1",
        )
        store = FakeStore(
            games=[{**GAME, "event_id": "evt-1"}],
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
                resolve_record,
                _receipt("evt-1", "rev-2"),
            ],
        )

        result = handle_request(
            {"action": "list_resolvable_games"}, store=store, now=_now()
        )

        assert result["games"] == []

    def test_excludes_deleted_events(self) -> None:
        delete_record = build_revision_event(
            game={**GAME, "event_id": "evt-1"},
            operation="delete",
            output=None,
            request_text="x",
            source="receptionist",
            now=_now(),
            git_base_sha="a" * 40,
            content_hash="hash",
            revision_id="rev-2",
        )
        store = FakeStore(
            games=[{**GAME, "event_id": "evt-1"}],
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
                delete_record,
                _receipt("evt-1", "rev-2"),
            ],
        )

        result = handle_request(
            {"action": "list_resolvable_games"}, store=store, now=_now()
        )

        assert result["games"] == []

    def test_includes_side_and_total_selection_for_the_user_to_review(
        self,
    ) -> None:
        store = FakeStore(
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
            ]
        )

        result = handle_request(
            {"action": "list_resolvable_games"}, store=store, now=_now()
        )

        game = result["games"][0]
        assert game["full_game_side_selection"] == "Phoenix Mercury"
        assert game["full_game_total_selection"] == "Over"
        assert game["away_score"] == "82"
        assert game["home_score"] == "96"


class TestResolvePreview:
    def test_includes_graded_outcome_when_score_is_final(self) -> None:
        store = FakeStore(
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
            ]
        )

        result = handle_request(
            {"action": "resolve_preview", "event_id": "evt-1"},
            store=store,
            now=_now(),
        )

        assert result["graded"]["side"]["result"] == "wrong"
        assert result["graded"]["total"]["result"] == "wrong"
        assert result["game"]["away_score"] == "82"

    def test_graded_is_none_when_not_yet_final(self) -> None:
        store = FakeStore(
            games=[{**GAME, "away_score": "", "home_score": "", "status": "scheduled"}],
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
            ],
        )

        result = handle_request(
            {"action": "resolve_preview", "event_id": "evt-1"},
            store=store,
            now=_now(),
        )

        assert result["graded"] is None
        assert result["game"]["status"] == "scheduled"

    def test_rejects_when_no_active_lean_exists(self) -> None:
        store = FakeStore(revision_events=[])

        with pytest.raises(ValueError, match="no resolvable active lean"):
            handle_request(
                {"action": "resolve_preview", "event_id": "evt-1"},
                store=store,
                now=_now(),
            )

    def test_rejects_unknown_event_id(self) -> None:
        store = FakeStore()

        with pytest.raises(ValueError, match="does not identify"):
            handle_request(
                {"action": "resolve_preview", "event_id": "does-not-exist"},
                store=store,
                now=_now(),
            )


class TestBuildResolution:
    def test_builds_a_skill_prompt_for_a_started_game(self) -> None:
        # The game commenced well over 12h before `now` -- build_resolution
        # must not reject it as "no longer current" the way generation does.
        store = FakeStore(
            revision_events=[
                _active_revision_event("evt-1", "rev-1"),
                _receipt("evt-1", "rev-1"),
            ]
        )

        result = handle_request(
            {
                "action": "build_resolution",
                "event_id": "evt-1",
                "matchup": "Phoenix Mercury @ Atlanta Dream",
            },
            store=store,
            now=_now(),
        )

        assert "resolve" in result["skill_prompt"].lower()
        assert "evt-1" in result["skill_prompt"]
        assert result["game"]["event_id"] == "evt-1"

    def test_rejects_mismatched_matchup(self) -> None:
        store = FakeStore()

        with pytest.raises(ValueError, match="does not match"):
            handle_request(
                {
                    "action": "build_resolution",
                    "event_id": "evt-1",
                    "matchup": "Wrong Team @ Other Team",
                },
                store=store,
                now=_now(),
            )
