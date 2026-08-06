from __future__ import annotations

import fcntl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wnba_poller.lean_cli import _repository_root, handle_request
from wnba_poller.lean_revisions import derive_revision_history

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
            "watch_conditions": ["Pace slows late"],
        },
    },
    "first_half": {},
    "summary": "Aces favored with room to grow.",
    "source_snapshot_ids": ["evt-1:2026-08-09T12:00:00Z:betonlineag"],
}


def _now() -> datetime:
    return datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self) -> None:
        self.game = dict(GAME)
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


class TestRepositoryRoot:
    def test_accepts_valid_repository(self, git_repo: Path) -> None:
        assert _repository_root(git_repo) == git_repo.resolve()

    def test_rejects_directory_without_git(self, tmp_path: Path) -> None:
        (tmp_path / "apps" / "wnba-poller").mkdir(parents=True)
        (tmp_path / "apps" / "wnba-poller" / "path210.md").write_text(
            "x", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="www repository root"):
            _repository_root(tmp_path)

    def test_rejects_directory_without_path210(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        with pytest.raises(ValueError, match="www repository root"):
            _repository_root(tmp_path)


class TestHandleRequestContext:
    def test_context_action_returns_deterministic_context(
        self, git_repo: Path
    ) -> None:
        store = FakeStore()
        result = handle_request(
            {
                "action": "context",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
            },
            store=store,
            repository=git_repo,
            now=_now(),
        )
        assert result["game"]["event_id"] == "evt-1"
        assert "path210" in result


class TestHandleRequestApply:
    def test_dry_run_apply_mutates_nothing(self, git_repo: Path) -> None:
        store = FakeStore()
        original = (
            git_repo / "apps" / "wnba-poller" / "path210.md"
        ).read_text(encoding="utf-8")

        result = handle_request(
            {
                "action": "apply",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
                "operation": "create",
                "request_text": "generate using standard template",
                "output": VALID_OUTPUT,
                "dry_run": True,
            },
            store=store,
            repository=git_repo,
            now=_now(),
        )

        assert result["dry_run"] is True
        assert store.revision_events == []
        assert (
            git_repo / "apps" / "wnba-poller" / "path210.md"
        ).read_text(encoding="utf-8") == original
        assert _git(["status", "--porcelain"], git_repo) == ""

    def test_real_apply_publishes_a_commit(self, git_repo: Path) -> None:
        store = FakeStore()
        result = handle_request(
            {
                "action": "apply",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
                "operation": "create",
                "request_text": "generate using standard template",
                "output": VALID_OUTPUT,
            },
            store=store,
            repository=git_repo,
            now=_now(),
        )
        assert "dry_run" not in result
        assert result["commit_sha"]
        assert len(store.revision_events) == 2  # revision + publication receipt
        assert _git(["rev-parse", "HEAD"], git_repo) == result["commit_sha"]

    def test_apply_rejects_when_lock_is_already_held(self, git_repo: Path) -> None:
        store = FakeStore()
        lock_path = git_repo / ".git" / "wnba-lean.lock"
        with lock_path.open("w", encoding="utf-8") as held_lock:
            fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="another WNBA lean workflow"):
                handle_request(
                    {
                        "action": "apply",
                        "event_id": "evt-1",
                        "matchup": "Indiana Fever @ Las Vegas Aces",
                        "operation": "create",
                        "request_text": "generate using standard template",
                        "output": VALID_OUTPUT,
                        "dry_run": True,
                    },
                    store=store,
                    repository=git_repo,
                    now=_now(),
                )
        assert store.revision_events == []

    def test_output_must_be_object_or_null(self, git_repo: Path) -> None:
        store = FakeStore()
        with pytest.raises(ValueError, match="output must be an object or null"):
            handle_request(
                {
                    "action": "apply",
                    "event_id": "evt-1",
                    "matchup": "Indiana Fever @ Las Vegas Aces",
                    "operation": "create",
                    "request_text": "generate using standard template",
                    "output": "not an object",
                },
                store=store,
                repository=git_repo,
                now=_now(),
            )


def _seed_resolvable_document(git_repo: Path) -> None:
    path210 = git_repo / "apps" / "wnba-poller" / "path210.md"
    path210.write_text(
        "# Notes For Model\n\nRules text.\n"
        "\n# Past Events\n\n"
        "1fadesparks\nwrong\nsome_tag\ncontext: monday. some context.\n"
        "\n# Model Cache\n\ncache stuff\n"
        "\n# Upcoming Events\n\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], git_repo)
    _git(["commit", "-m", "seed resolution fixture"], git_repo)
    _git(["push", "origin", "main"], git_repo)


class TestHandleRequestResolve:
    def test_context_requires_allow_started_for_a_finished_game(
        self, git_repo: Path
    ) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore()
        long_after_tip = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="no longer current"):
            handle_request(
                {
                    "action": "context",
                    "event_id": "evt-1",
                    "matchup": "Indiana Fever @ Las Vegas Aces",
                },
                store=store,
                repository=git_repo,
                now=long_after_tip,
            )

        result = handle_request(
            {
                "action": "context",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
                "allow_started": True,
            },
            store=store,
            repository=git_repo,
            now=long_after_tip,
        )
        assert result["game"]["event_id"] == "evt-1"

    def test_resolve_action_grades_and_publishes(self, git_repo: Path) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore()
        create_result = handle_request(
            {
                "action": "apply",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
                "operation": "create",
                "request_text": "generate using standard template",
                "output": VALID_OUTPUT,
            },
            store=store,
            repository=git_repo,
            now=_now(),
        )

        store.game.update(
            {
                "away_score": "78",
                "home_score": "85",
                "latest_away_spread": "0",
                "latest_home_spread": "0",
                "latest_total": "160",
            }
        )
        long_after_tip = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)

        result = handle_request(
            {
                "action": "resolve",
                "event_id": "evt-1",
                "matchup": "Indiana Fever @ Las Vegas Aces",
                "entry_slug": "fadeaces",
                "tags": "back_favorite",
                "line_movement": "aces pick'em throughout",
                "context_text": "context: sunday. resolved the aces lean.",
                "model_lean_text": "side (ACES pick'em) -- HIT; total (OVER 160) -- HIT.",
                "request_text": "Resolve using the final score.",
            },
            store=store,
            repository=git_repo,
            now=long_after_tip,
        )

        assert result["operation"] == "resolve"
        # Aces (home) won 85-78 at a pick'em line -- side HIT.
        assert result["result"] == "right"
        assert result["entry_name"] == "2fadeaces"

        content = (
            git_repo / "apps" / "wnba-poller" / "path210.md"
        ).read_text(encoding="utf-8")
        assert "WNBA_LEAN_EVENT_START" not in content
        assert "2fadeaces" in content

        history = store.read_game_history(event_id="evt-1")
        assert history["active_revision"]["revision_id"] == (
            result["revision_id"]
        )
        assert history["active_revision"]["effective_status"] == "resolved"

    def test_resolve_rejects_missing_entry_slug(self, git_repo: Path) -> None:
        _seed_resolvable_document(git_repo)
        store = FakeStore()
        with pytest.raises(ValueError, match="entry_slug is invalid"):
            handle_request(
                {
                    "action": "resolve",
                    "event_id": "evt-1",
                    "matchup": "Indiana Fever @ Las Vegas Aces",
                    "tags": "tag",
                    "context_text": "context: x.",
                    "request_text": "x",
                },
                store=store,
                repository=git_repo,
                now=_now(),
            )


class TestHandleRequestValidation:
    def test_rejects_unsupported_action(self, git_repo: Path) -> None:
        store = FakeStore()
        with pytest.raises(ValueError, match="unsupported WNBA lean workflow action"):
            handle_request(
                {
                    "action": "delete_everything",
                    "event_id": "evt-1",
                    "matchup": "Indiana Fever @ Las Vegas Aces",
                },
                store=store,
                repository=git_repo,
                now=_now(),
            )

    def test_rejects_missing_event_id(self, git_repo: Path) -> None:
        store = FakeStore()
        with pytest.raises(ValueError, match="event_id is invalid"):
            handle_request(
                {"action": "context", "matchup": "Indiana Fever @ Las Vegas Aces"},
                store=store,
                repository=git_repo,
                now=_now(),
            )
