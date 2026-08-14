from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wnba_poller.lean_revisions import derive_revision_history
from wnba_poller.lean_workflow import (
    GitPublisher,
    _run_command,
    build_request_template,
    build_skill_prompt,
    execute_resolution,
    execute_revision,
    parse_request_template,
    validate_lean_output,
)

GAME = {
    "event_id": "evt-1",
    "espn_event_id": "espn-1",
    "commence_time_utc": "2026-08-10T23:00:00Z",
    "commence_time_et": "2026-08-10T19:00:00-04:00",
    "away_team": "Indiana Fever",
    "home_team": "Las Vegas Aces",
}

VALID_OUTPUT: dict[str, Any] = {
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
            "watch_conditions": ["Pace slows if starters rest early"],
        },
    },
    "first_half": {},
    "summary": "Aces favored with room to grow.",
    "source_snapshot_ids": ["evt-1:2026-08-09T12:00:00Z:betonlineag"],
    "stars": {"side": 2, "total": 1},
}


def _now() -> datetime:
    return datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, game: dict[str, Any] | None = None) -> None:
        self.game = dict(game or GAME)
        self.revision_events: list[dict[str, Any]] = []

    def read_games(self) -> list[dict[str, Any]]:
        return [self.game]

    def read_snapshots_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [
            {
                "captured_at_utc": "2026-08-09T12:00:00Z",
                "bookmaker": "betonlineag",
            }
        ]

    def read_thoughts_for_event(
        self, *, event_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return []

    def read_game_history(self, *, event_id: str) -> dict[str, Any]:
        return derive_revision_history(self.revision_events, event_id=event_id)

    def append_lean_revision_event(self, record: dict[str, Any]) -> bool:
        key = record.get("record_id")
        if any(existing.get("record_id") == key for existing in self.revision_events):
            return False
        self.revision_events.append(record)
        return True


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare"], origin)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["remote", "add", "origin", str(origin)], work)

    app_dir = work / "apps" / "wnba-poller"
    app_dir.mkdir(parents=True)
    (app_dir / "path210.md").write_text(
        "# path210\n\nCurated WNBA decision log.\n", encoding="utf-8"
    )
    _git(["add", "-A"], work)
    _git(["commit", "-m", "init"], work)
    _git(["push", "-u", "origin", "main"], work)
    return work


class TestGitPublisher:
    def test_publish_succeeds_when_only_path210_is_dirty(self, git_repo: Path) -> None:
        publisher = GitPublisher(repository=git_repo)
        base_sha = publisher.precondition()
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        path210.write_text(
            path210.read_text(encoding="utf-8") + "\nnew content\n", encoding="utf-8"
        )
        commit_sha = publisher.publish(
            path=path210,
            expected_base_sha=base_sha,
            commit_message="Update WNBA lean for Aces at Fever",
        )
        assert commit_sha != base_sha
        assert _git(["log", "-1", "--pretty=%H"], git_repo) == commit_sha
        assert _git(["rev-parse", "origin/main"], git_repo) == commit_sha

    def test_publish_rejects_unrelated_dirty_files(self, git_repo: Path) -> None:
        publisher = GitPublisher(repository=git_repo)
        base_sha = publisher.precondition()
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        path210.write_text(
            path210.read_text(encoding="utf-8") + "\nnew content\n", encoding="utf-8"
        )
        (git_repo / "unrelated.txt").write_text("oops", encoding="utf-8")
        with pytest.raises(ValueError, match="unrelated working tree changes"):
            publisher.publish(
                path=path210, expected_base_sha=base_sha, commit_message="x"
            )

    def test_publish_rejects_stale_base(self, git_repo: Path) -> None:
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        path210.write_text(
            path210.read_text(encoding="utf-8") + "\nnew content\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="expected base"):
            publisher.publish(
                path=path210, expected_base_sha="0" * 40, commit_message="x"
            )

    def test_publish_rejects_wrong_branch(self, git_repo: Path) -> None:
        publisher = GitPublisher(repository=git_repo)
        base_sha = publisher.precondition()
        _git(["checkout", "-b", "other"], git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        path210.write_text(
            path210.read_text(encoding="utf-8") + "\nnew content\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="branch must be"):
            publisher.publish(
                path=path210, expected_base_sha=base_sha, commit_message="x"
            )

    def test_publish_rejects_paths_outside_path210(self, git_repo: Path) -> None:
        publisher = GitPublisher(repository=git_repo)
        base_sha = publisher.precondition()
        other = git_repo / "apps" / "wnba-poller" / "README.md"
        other.write_text("nope", encoding="utf-8")
        with pytest.raises(ValueError, match="may only change path210.md"):
            publisher.publish(
                path=other, expected_base_sha=base_sha, commit_message="x"
            )

    def test_precondition_before_write_still_requires_fully_clean_tree(
        self, git_repo: Path
    ) -> None:
        publisher = GitPublisher(repository=git_repo)
        (git_repo / "unrelated.txt").write_text("oops", encoding="utf-8")
        with pytest.raises(ValueError, match="not clean"):
            publisher.precondition()


class TestExecuteRevisionLifecycle:
    def test_create_revise_delete_undo(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        create_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=VALID_OUTPUT,
        )
        assert create_result["operation"] == "create"
        content = path210.read_text(encoding="utf-8")
        assert "evt-1" in content
        assert "Las Vegas Aces" in content
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == create_result["revision_id"]

        revised_output = {
            **VALID_OUTPUT,
            "summary": "Updated take: Aces still favored.",
        }
        revise_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=revised_output,
        )
        assert revise_result["operation"] == "revise"
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == revise_result["revision_id"]
        assert len(history["revision_history"]) == 2
        assert "Updated take" in path210.read_text(encoding="utf-8")

        delete_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="delete",
            request_text="delete the lean",
            source="receptionist",
            now=_now(),
        )
        assert delete_result["operation"] == "delete"
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"] is None
        assert history["revision_history"][-1]["effective_status"] == "deleted"

        undo_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="undo",
            request_text="undo latest",
            source="receptionist",
            now=_now(),
        )
        assert undo_result["operation"] == "undo"
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == undo_result["revision_id"]
        assert "Updated take" in path210.read_text(encoding="utf-8")

        commits = _git(["log", "--oneline"], git_repo).splitlines()
        assert len(commits) == 5

        record_types = [record["record_type"] for record in store.revision_events]
        assert record_types.count("revision") == 4
        assert record_types.count("publication_receipt") == 4

    def test_delete_without_active_lean_fails(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        with pytest.raises(ValueError, match="cannot delete without an active lean"):
            execute_revision(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                operation="delete",
                request_text="delete the lean",
                source="receptionist",
                now=_now(),
            )

    def test_undo_without_history_fails(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        with pytest.raises(ValueError, match="no published lean revision to undo"):
            execute_revision(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                operation="undo",
                request_text="undo latest",
                source="receptionist",
                now=_now(),
            )


class TestDryRun:
    def test_dry_run_create_mutates_nothing(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        original_content = path210.read_text(encoding="utf-8")
        head_before = _git(["rev-parse", "HEAD"], git_repo)

        result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=VALID_OUTPUT,
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["operation"] == "create"
        assert result["event_id"] == "evt-1"
        assert "Las Vegas Aces" in result["proposed_block"]
        assert (
            result["normalized_output"]["full_game"]["side"]["selection"]
            == "Las Vegas Aces"
        )
        assert result["context"]["game"]["event_id"] == "evt-1"
        assert result["proposed_revision_event"]["operation"] == "create"

        # No mutation of any kind occurred.
        assert path210.read_text(encoding="utf-8") == original_content
        assert store.revision_events == []
        assert _git(["status", "--porcelain"], git_repo) == ""
        assert _git(["rev-parse", "HEAD"], git_repo) == head_before

    def test_dry_run_revise_previews_against_existing_active_lean(
        self, git_repo: Path
    ) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        create_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=VALID_OUTPUT,
        )
        content_after_create = path210.read_text(encoding="utf-8")
        head_after_create = _git(["rev-parse", "HEAD"], git_repo)

        dry_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output={**VALID_OUTPUT, "summary": "Dry-run preview only."},
            dry_run=True,
        )

        assert dry_result["operation"] == "revise"
        assert "Dry-run preview only." in dry_result["proposed_block"]
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == (
            create_result["revision_id"]
        )
        assert path210.read_text(encoding="utf-8") == content_after_create
        assert "Dry-run preview only." not in path210.read_text(encoding="utf-8")
        assert _git(["rev-parse", "HEAD"], git_repo) == head_after_create

    def test_dry_run_never_acquires_publish_lock_state(
        self, git_repo: Path
    ) -> None:
        store = FakeStore()

        def exploding_runner(args: Any, cwd: Path) -> Any:
            if tuple(args[:2]) in {("git", "commit"), ("git", "push")}:
                raise AssertionError(
                    "dry run must never commit or push"
                )
            return _run_command(args, cwd)

        publisher = GitPublisher(repository=git_repo, runner=exploding_runner)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=VALID_OUTPUT,
            dry_run=True,
        )


def _seed_resolvable_document(git_repo: Path) -> None:
    path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
    path210.write_text(
        "# Notes For Model\n\nRules text.\n"
        "\n# Past Events\n\n"
        "1fadesparks\nwrong\nsome_tag\ncontext: monday. some context.\n"
        "\n# Model Cache\n\nSignal right/wrong record (based on tags):\n"
        "back_favorite: 0 right / 0 wrong\n"
        "\n# Upcoming Events\n\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], git_repo)
    _git(["commit", "-m", "seed resolution fixture"], git_repo)
    _git(["push", "origin", "main"], git_repo)


class TestExecuteResolution:
    def test_resolves_using_the_real_mercury_dream_result(
        self, git_repo: Path
    ) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore(
            game={
                **GAME,
                "away_team": "Phoenix Mercury",
                "home_team": "Atlanta Dream",
            }
        )
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        create_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output={
                **VALID_OUTPUT,
                "full_game": {
                    "side": {
                        **VALID_OUTPUT["full_game"]["side"],
                        "selection": "Phoenix Mercury",
                    },
                    "total": VALID_OUTPUT["full_game"]["total"],
                },
            },
        )

        # The game has since gone final -- update the store the same way
        # a real backfill would (never touched by execute_revision above).
        store.game.update(
            {
                "away_score": "82",
                "home_score": "96",
                "latest_away_spread": "7",
                "latest_home_spread": "-7",
                "latest_total": "181.5",
            }
        )

        resolve_result = execute_resolution(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            entry_slug="fademercury",
            tags="back_favorite,follow_line_movement",
            line_movement="dream -5.5 (open) -> -7 (close)",
            context_text="context: wednesday. faded the dream cover.",
            model_lean_text="side (MERCURY +7) -- MISS; total (OVER 181.5) -- MISS.",
            request_text="Resolve using the final score.",
            source="claude-skill",
            now=_now(),
        )

        assert resolve_result["operation"] == "resolve"
        assert resolve_result["result"] == "wrong"
        assert resolve_result["entry_name"] == "2fademercury"

        content = path210.read_text(encoding="utf-8")
        assert "WNBA_LEAN_EVENT_START" not in content
        assert "2fademercury" in content
        assert content.index("2fademercury") < content.index("# Model Cache")
        # resolution rebuilt the Model Cache (path210 rule #2): the resolved
        # entry carries back_favorite and graded wrong, so its cache count
        # moved 0/0 -> 0/1 in the same commit.
        assert "back_favorite: 0 right / 1 wrong" in content

        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == (
            resolve_result["revision_id"]
        )
        assert history["active_revision"]["effective_status"] == "resolved"
        assert history["active_revision"]["operation"] == "resolve"

        commits = _git(["log", "--oneline"], git_repo).splitlines()
        assert len(commits) == 4  # init + seed + create + resolve

    def test_resolves_a_game_more_than_twelve_hours_after_commence(
        self, git_repo: Path
    ) -> None:
        # Regression test: resolve_current_game's ordinary 12h-post-commence
        # cutoff (correctly used to stop *generating* a fresh pregame lean
        # for a long-finished game) must NOT block *resolving* one -- that
        # is the whole point of resolution, and it is routinely done the
        # next day, well past 12h. GAME commences 2026-08-10T23:00:00Z;
        # resolve at 2026-08-12T01:00:00Z, ~26 hours later.
        _seed_resolvable_document(git_repo)
        store = FakeStore(
            game={
                **GAME,
                "away_team": "Phoenix Mercury",
                "home_team": "Atlanta Dream",
            }
        )
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output={
                **VALID_OUTPUT,
                "full_game": {
                    "side": {
                        **VALID_OUTPUT["full_game"]["side"],
                        "selection": "Phoenix Mercury",
                    },
                    "total": VALID_OUTPUT["full_game"]["total"],
                },
            },
        )
        store.game.update(
            {
                "away_score": "82",
                "home_score": "96",
                "latest_away_spread": "7",
                "latest_home_spread": "-7",
                "latest_total": "181.5",
            }
        )
        next_day = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)

        resolve_result = execute_resolution(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            entry_slug="fademercury",
            tags="tag",
            line_movement="",
            context_text="context: wednesday. resolved the next day.",
            model_lean_text="",
            request_text="Resolve the next day.",
            source="claude-skill",
            now=next_day,
        )

        assert resolve_result["result"] == "wrong"

    def test_cannot_resolve_without_an_active_lean(self, git_repo: Path) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        with pytest.raises(ValueError, match="cannot resolve without"):
            execute_resolution(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                entry_slug="fadeaces",
                tags="tag",
                line_movement="",
                context_text="context: x.",
                model_lean_text="",
                request_text="x",
                source="claude-skill",
                now=_now(),
            )

    def test_cannot_resolve_twice(self, git_repo: Path) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore(
            game={
                **GAME,
                "away_team": "Phoenix Mercury",
                "home_team": "Atlanta Dream",
            }
        )
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output={
                **VALID_OUTPUT,
                "full_game": {
                    "side": {
                        **VALID_OUTPUT["full_game"]["side"],
                        "selection": "Phoenix Mercury",
                    },
                    "total": VALID_OUTPUT["full_game"]["total"],
                },
            },
        )
        store.game.update(
            {
                "away_score": "82",
                "home_score": "96",
                "latest_away_spread": "7",
                "latest_home_spread": "-7",
                "latest_total": "181.5",
            }
        )
        kwargs = dict(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            entry_slug="fademercury",
            tags="tag",
            line_movement="",
            context_text="context: x.",
            model_lean_text="",
            request_text="x",
            source="claude-skill",
            now=_now(),
        )
        execute_resolution(**kwargs)

        with pytest.raises(ValueError, match="already been resolved"):
            execute_resolution(**kwargs)

    def test_dry_run_style_failure_does_not_corrupt_state(
        self, git_repo: Path
    ) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore(
            game={
                **GAME,
                "away_team": "Phoenix Mercury",
                "home_team": "Atlanta Dream",
            }
        )
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        create_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Phoenix Mercury @ Atlanta Dream",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output={
                **VALID_OUTPUT,
                "full_game": {
                    "side": {
                        **VALID_OUTPUT["full_game"]["side"],
                        "selection": "Phoenix Mercury",
                    },
                    "total": VALID_OUTPUT["full_game"]["total"],
                },
            },
        )
        content_after_create = path210.read_text(encoding="utf-8")
        # Game has not actually gone final yet -- no score recorded.
        with pytest.raises(ValueError, match="no recorded final score"):
            execute_resolution(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Phoenix Mercury @ Atlanta Dream",
                entry_slug="fademercury",
                tags="tag",
                line_movement="",
                context_text="context: x.",
                model_lean_text="",
                request_text="x",
                source="claude-skill",
                now=_now(),
            )
        assert path210.read_text(encoding="utf-8") == content_after_create
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == (
            create_result["revision_id"]
        )
        assert history["active_revision"]["effective_status"] == "active"


class TestExecuteRevisionGuards:
    def test_mismatched_matchup_fails_before_any_mutation(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        before = path210.read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="matchup"):
            execute_revision(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Wrong Team @ Other Team",
                operation="create",
                request_text="x",
                source="receptionist",
                now=_now(),
                output=VALID_OUTPUT,
            )
        assert store.revision_events == []
        assert path210.read_text(encoding="utf-8") == before
        assert _git(["status", "--porcelain"], git_repo) == ""

    def test_started_game_fails(self, git_repo: Path) -> None:
        store = FakeStore(
            game={**GAME, "commence_time_utc": "2026-08-01T00:00:00Z"}
        )
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        with pytest.raises(ValueError, match="no longer current"):
            execute_revision(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                operation="create",
                request_text="x",
                source="receptionist",
                now=_now(),
                output=VALID_OUTPUT,
            )
        assert store.revision_events == []


class TestFailureRecovery:
    def test_failed_push_restores_state_and_appends_abort_receipt(
        self, git_repo: Path
    ) -> None:
        store = FakeStore()

        def failing_runner(args: Any, cwd: Path) -> Any:
            if tuple(args[:2]) == ("git", "push"):
                raise RuntimeError("network unavailable")
            return _run_command(args, cwd)

        publisher = GitPublisher(repository=git_repo, runner=failing_runner)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
        original_content = path210.read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="network unavailable"):
            execute_revision(
                store=store,
                publisher=publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                operation="create",
                request_text="generate using standard template",
                source="receptionist",
                now=_now(),
                output=VALID_OUTPUT,
            )

        assert path210.read_text(encoding="utf-8") == original_content
        record_types = [record["record_type"] for record in store.revision_events]
        assert record_types == ["revision", "abort_receipt"]
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"] is None
        assert _git(["status", "--porcelain"], git_repo) == ""
        assert _git(["rev-parse", "HEAD"], git_repo) == _git(
            ["rev-parse", "origin/main"], git_repo
        )

    def test_prior_active_lean_survives_a_failed_revise(self, git_repo: Path) -> None:
        store = FakeStore()
        publisher = GitPublisher(repository=git_repo)
        path210 = git_repo / "apps" / "wnba-poller" / "path210.md"

        create_result = execute_revision(
            store=store,
            publisher=publisher,
            path210_path=path210,
            event_id="evt-1",
            expected_matchup="Indiana Fever @ Las Vegas Aces",
            operation="create",
            request_text="generate using standard template",
            source="receptionist",
            now=_now(),
            output=VALID_OUTPUT,
        )
        content_after_create = path210.read_text(encoding="utf-8")

        def failing_runner(args: Any, cwd: Path) -> Any:
            if tuple(args[:2]) == ("git", "commit"):
                raise RuntimeError("commit hook rejected")
            return _run_command(args, cwd)

        failing_publisher = GitPublisher(repository=git_repo, runner=failing_runner)
        with pytest.raises(RuntimeError, match="commit hook rejected"):
            execute_revision(
                store=store,
                publisher=failing_publisher,
                path210_path=path210,
                event_id="evt-1",
                expected_matchup="Indiana Fever @ Las Vegas Aces",
                operation="create",
                request_text="generate using standard template",
                source="receptionist",
                now=_now(),
                output={**VALID_OUTPUT, "summary": "This should not stick."},
            )

        assert path210.read_text(encoding="utf-8") == content_after_create
        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == create_result["revision_id"]


class TestRequestTemplate:
    def test_build_and_parse_round_trip(self) -> None:
        template = build_request_template(
            event_id="evt-1",
            away_team="Indiana Fever",
            home_team="Las Vegas Aces",
            additional_thoughts="Watch the injury report",
        )
        assert parse_request_template(template) == {
            "event_id": "evt-1",
            "matchup": "Indiana Fever @ Las Vegas Aces",
            "request": "generate using standard template",
            "additional_thoughts": "Watch the injury report",
        }

    def test_generate_now_and_pasted_template_normalize_identically(self) -> None:
        template = build_request_template(
            event_id="evt-1", away_team="Indiana Fever", home_team="Las Vegas Aces"
        )
        generate_now_request = {
            "event_id": "evt-1",
            "matchup": "Indiana Fever @ Las Vegas Aces",
            "request": "generate using standard template",
            "additional_thoughts": "",
        }
        assert parse_request_template(template) == generate_now_request

    def test_build_skill_prompt_embeds_validated_template(self) -> None:
        template = build_request_template(
            event_id="evt-1", away_team="Indiana Fever", home_team="Las Vegas Aces"
        )
        prompt = build_skill_prompt(template)
        assert "wnba-lean skill" in prompt
        assert template in prompt

    def test_build_skill_prompt_rejects_non_template_text(self) -> None:
        with pytest.raises(ValueError):
            build_skill_prompt("not a template")

    def test_parse_rejects_mismatched_action(self) -> None:
        bad = (
            "WNBA_LEAN_REQUEST_V1\n"
            "event_id: evt-1\n"
            "matchup: A @ B\n"
            "request: do something else\n"
            "additional_thoughts:\n"
        )
        with pytest.raises(ValueError):
            parse_request_template(bad)

    def test_parse_rejects_non_template_text(self) -> None:
        assert parse_request_template("just a regular message") is None


class TestValidateLeanOutput:
    ALLOWED_SNAPSHOTS = {"evt-1:2026-08-09T12:00:00Z:betonlineag"}

    def test_accepts_well_formed_output(self) -> None:
        normalized = validate_lean_output(
            VALID_OUTPUT, game=GAME, allowed_snapshot_ids=self.ALLOWED_SNAPSHOTS
        )
        assert normalized["full_game"]["side"]["selection"] == "Las Vegas Aces"
        assert normalized["first_half"] == {}

    def test_rejects_unknown_snapshot_id(self) -> None:
        bad = {**VALID_OUTPUT, "source_snapshot_ids": ["unknown"]}
        with pytest.raises(ValueError, match="unknown snapshot"):
            validate_lean_output(
                bad, game=GAME, allowed_snapshot_ids=self.ALLOWED_SNAPSHOTS
            )

    def test_rejects_side_not_matching_teams(self) -> None:
        bad = {
            **VALID_OUTPUT,
            "full_game": {
                **VALID_OUTPUT["full_game"],
                "side": {
                    **VALID_OUTPUT["full_game"]["side"],
                    "selection": "Some Other Team",
                },
            },
        }
        with pytest.raises(ValueError, match="side selection is invalid"):
            validate_lean_output(
                bad, game=GAME, allowed_snapshot_ids=self.ALLOWED_SNAPSHOTS
            )

    def test_rejects_invalid_strength(self) -> None:
        bad = {
            **VALID_OUTPUT,
            "full_game": {
                **VALID_OUTPUT["full_game"],
                "total": {
                    **VALID_OUTPUT["full_game"]["total"],
                    "strength": "extremely confident",
                },
            },
        }
        with pytest.raises(ValueError, match="strength is invalid"):
            validate_lean_output(
                bad, game=GAME, allowed_snapshot_ids=self.ALLOWED_SNAPSHOTS
            )

    def test_accepts_evidence_supported_first_half(self) -> None:
        with_first_half = {
            **VALID_OUTPUT,
            "first_half": {
                "total": {
                    "selection": "Under",
                    "strength": "watch",
                    "evidence": ["Slow starts in last three meetings"],
                    "watch_conditions": ["Reverses if starters confirmed healthy"],
                }
            },
        }
        normalized = validate_lean_output(
            with_first_half, game=GAME, allowed_snapshot_ids=self.ALLOWED_SNAPSHOTS
        )
        assert normalized["first_half"]["total"]["selection"] == "Under"
        assert "side" not in normalized["first_half"]
