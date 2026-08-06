from __future__ import annotations

from datetime import datetime, timezone

from wnba_poller.lean_revisions import (
    build_abort_receipt,
    build_publication_receipt,
    build_revision_event,
    derive_revision_history,
    revision_to_output,
)

GAME = {
    "event_id": "evt-1",
    "espn_event_id": "espn-1",
    "commence_time_utc": "2026-08-10T23:00:00Z",
    "commence_time_et": "2026-08-10T19:00:00-04:00",
    "away_team": "Indiana Fever",
    "home_team": "Las Vegas Aces",
}

OUTPUT = {
    "full_game": {
        "side": {
            "selection": "Las Vegas Aces",
            "strength": "moderate",
            "evidence": ["Line moved toward Aces"],
            "watch_conditions": ["Injury report"],
        },
        "total": {
            "selection": "Over",
            "strength": "small",
            "evidence": ["High pace matchup"],
            "watch_conditions": ["Pace slows late"],
        },
    },
    "first_half": {},
    "summary": "Aces favored with room to grow.",
    "source_snapshot_ids": ["evt-1:2026-08-09T12:00:00Z:betonlineag"],
}


def _now(offset_minutes: int = 0) -> datetime:
    base = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    return base.replace(minute=offset_minutes % 60)


def _revision(revision_id: str, *, operation: str = "create", output=OUTPUT, **overrides):
    record = build_revision_event(
        game=GAME,
        operation=operation,
        output=output,
        request_text="generate using standard template",
        source="receptionist",
        now=_now(),
        git_base_sha="a" * 40,
        content_hash="hash",
        revision_id=revision_id,
    )
    record.update(overrides)
    return record


class TestBuildRevisionEvent:
    def test_populates_choice_fields_from_output(self) -> None:
        record = _revision("rev-1")
        assert record["record_type"] == "revision"
        assert record["full_game_side_selection"] == "Las Vegas Aces"
        assert record["full_game_side_strength"] == "moderate"
        assert record["full_game_total_selection"] == "Over"
        assert record["first_half_side_selection"] == ""
        assert record["resulting_status"] == "active"

    def test_delete_operation_has_no_choice_fields_and_deleted_status(self) -> None:
        record = _revision("rev-1", operation="delete", output=None)
        assert record["resulting_status"] == "deleted"
        assert record["full_game_side_selection"] == ""
        assert record["summary"] == ""

    def test_rejects_unsupported_operation(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="unsupported"):
            build_revision_event(
                game=GAME,
                operation="rename",
                output=OUTPUT,
                request_text="x",
                source="receptionist",
                now=_now(),
                git_base_sha="a" * 40,
                content_hash="hash",
                revision_id="rev-1",
            )


class TestDeriveRevisionHistory:
    def test_unpublished_revision_is_excluded_from_active(self) -> None:
        revision = _revision("rev-1")
        history = derive_revision_history([revision], event_id="evt-1")
        assert history["active_revision"] is None
        assert history["unpublished_revision_ids"] == ["rev-1"]
        assert history["revision_history"] == []

    def test_published_revision_becomes_active(self) -> None:
        revision = _revision("rev-1")
        receipt = build_publication_receipt(
            revision_id="rev-1",
            event_id="evt-1",
            commit_sha="c" * 40,
            branch="main",
            now=_now(),
        )
        history = derive_revision_history([revision, receipt], event_id="evt-1")
        assert history["active_revision"]["revision_id"] == "rev-1"
        assert history["active_revision"]["git_commit_sha"] == "c" * 40
        assert history["revision_history"][0]["effective_status"] == "active"

    def test_aborted_revision_is_excluded_even_if_a_receipt_exists_later(
        self,
    ) -> None:
        revision = _revision("rev-1")
        abort = build_abort_receipt(
            revision_id="rev-1", event_id="evt-1", error="commit failed", now=_now()
        )
        history = derive_revision_history([revision, abort], event_id="evt-1")
        assert history["active_revision"] is None
        assert history["aborted_revision_ids"] == ["rev-1"]
        assert history["revision_history"] == []

    def test_revise_supersedes_prior_active(self) -> None:
        create = _revision("rev-1", requested_at_utc="2026-08-09T13:00:00Z")
        create["requested_at_utc"] = "2026-08-09T13:00:00Z"
        receipt_1 = build_publication_receipt(
            revision_id="rev-1",
            event_id="evt-1",
            commit_sha="c" * 40,
            branch="main",
            now=_now(),
        )
        revise = _revision("rev-2", operation="revise")
        revise["requested_at_utc"] = "2026-08-09T14:00:00Z"
        receipt_2 = build_publication_receipt(
            revision_id="rev-2",
            event_id="evt-1",
            commit_sha="d" * 40,
            branch="main",
            now=_now(),
        )
        history = derive_revision_history(
            [create, receipt_1, revise, receipt_2], event_id="evt-1"
        )
        assert history["active_revision"]["revision_id"] == "rev-2"
        statuses = {
            record["revision_id"]: record["effective_status"]
            for record in history["revision_history"]
        }
        assert statuses == {"rev-1": "superseded", "rev-2": "active"}

    def test_delete_clears_active_and_marks_deleted(self) -> None:
        create = _revision("rev-1")
        create["requested_at_utc"] = "2026-08-09T13:00:00Z"
        receipt_1 = build_publication_receipt(
            revision_id="rev-1",
            event_id="evt-1",
            commit_sha="c" * 40,
            branch="main",
            now=_now(),
        )
        delete = _revision("rev-2", operation="delete", output=None)
        delete["requested_at_utc"] = "2026-08-09T14:00:00Z"
        receipt_2 = build_publication_receipt(
            revision_id="rev-2",
            event_id="evt-1",
            commit_sha="d" * 40,
            branch="main",
            now=_now(),
        )
        history = derive_revision_history(
            [create, receipt_1, delete, receipt_2], event_id="evt-1"
        )
        assert history["active_revision"] is None
        statuses = {
            record["revision_id"]: record["effective_status"]
            for record in history["revision_history"]
        }
        assert statuses == {"rev-1": "superseded", "rev-2": "deleted"}

    def test_other_event_records_are_ignored(self) -> None:
        revision = _revision("rev-1")
        receipt = build_publication_receipt(
            revision_id="rev-1",
            event_id="evt-1",
            commit_sha="c" * 40,
            branch="main",
            now=_now(),
        )
        history = derive_revision_history(
            [revision, receipt], event_id="evt-other"
        )
        assert history["active_revision"] is None
        assert history["revision_history"] == []


class TestRevisionToOutput:
    def test_round_trips_full_and_first_half_choices(self) -> None:
        with_first_half = {
            **OUTPUT,
            "first_half": {
                "total": {
                    "selection": "Under",
                    "strength": "watch",
                    "evidence": ["Slow starts"],
                    "watch_conditions": ["Starters confirmed healthy"],
                }
            },
        }
        record = build_revision_event(
            game=GAME,
            operation="create",
            output=with_first_half,
            request_text="x",
            source="receptionist",
            now=_now(),
            git_base_sha="a" * 40,
            content_hash="hash",
            revision_id="rev-1",
        )
        restored = revision_to_output(record)
        assert restored["full_game"]["side"]["selection"] == "Las Vegas Aces"
        assert restored["full_game"]["total"]["evidence"] == [
            "High pace matchup"
        ]
        assert restored["first_half"]["total"]["selection"] == "Under"
        assert "side" not in restored["first_half"]
        assert restored["source_snapshot_ids"] == OUTPUT["source_snapshot_ids"]

    def test_missing_selection_yields_none_choice(self) -> None:
        record = build_revision_event(
            game=GAME,
            operation="create",
            output=OUTPUT,
            request_text="x",
            source="receptionist",
            now=_now(),
            git_base_sha="a" * 40,
            content_hash="hash",
            revision_id="rev-1",
        )
        restored = revision_to_output(record)
        assert restored["first_half"] == {}


class TestResolveOperation:
    def test_resolve_sets_resulting_status_resolved(self) -> None:
        record = _revision("rev-1", operation="resolve")
        assert record["resulting_status"] == "resolved"

    def test_resolve_is_a_valid_operation(self) -> None:
        # Should not raise -- "resolve" needed no new Sheet columns, it
        # reuses operation/resulting_status/summary like every other op.
        build_revision_event(
            game=GAME,
            operation="resolve",
            output=OUTPUT,
            request_text="x",
            source="receptionist",
            now=_now(),
            git_base_sha="a" * 40,
            content_hash="hash",
            revision_id="rev-1",
        )

    def test_resolved_revision_still_surfaces_as_active_revision(self) -> None:
        create = _revision("rev-1")
        create["requested_at_utc"] = "2026-08-05T22:00:00Z"
        receipt_1 = build_publication_receipt(
            revision_id="rev-1",
            event_id="evt-1",
            commit_sha="c" * 40,
            branch="main",
            now=_now(),
        )
        resolve = _revision("rev-2", operation="resolve")
        resolve["requested_at_utc"] = "2026-08-06T02:00:00Z"
        resolve["supersedes_revision_id"] = "rev-1"
        receipt_2 = build_publication_receipt(
            revision_id="rev-2",
            event_id="evt-1",
            commit_sha="d" * 40,
            branch="main",
            now=_now(),
        )

        history = derive_revision_history(
            [create, receipt_1, resolve, receipt_2], event_id="evt-1"
        )

        assert history["active_revision"]["revision_id"] == "rev-2"
        assert history["active_revision"]["effective_status"] == "resolved"
        statuses = {
            record["revision_id"]: record["effective_status"]
            for record in history["revision_history"]
        }
        assert statuses == {"rev-1": "superseded", "rev-2": "resolved"}
