import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from receptionist2 import memory_monitor
from receptionist2.control import reauth_command
from receptionist2.database import Database
from receptionist2.deployments import is_self_deployment, parse_request_file
from receptionist2.gateway import (
    markup_from_json,
    progress_text,
    session_topics_text,
)
from receptionist2.investigation import (
    REQUIRED_SECTIONS,
    continuation_prompt,
    investigation_prompt,
    missing_sections,
)
from receptionist2.maintenance import run_maintenance
from receptionist2.provider import assistant_activities
from receptionist2.resources import meminfo, resource_summary
from receptionist2.watchdog import extract_auth_url


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "receptionist2.db")
    database.initialize()
    return database


def accept(database: Database, message_id: int, text: str = "work") -> dict:
    session = database.ensure_user(
        1, 2, default_model="claude-opus-5-5", default_effort="high"
    )
    run, _ = database.accept_message(
        user_id=1,
        chat_id=2,
        message_id=message_id,
        text=text,
        default_model=session["model"],
        default_effort=session["effort"],
    )
    return run


def test_investigation_requires_every_section() -> None:
    prompt = investigation_prompt("  worker is stuck  ")
    assert "worker is stuck" in prompt
    for name in REQUIRED_SECTIONS:
        assert f"## {name}" in prompt
    report = "## Findings\nx\n### Evidence\ny\n## changes\nnone\n"
    assert missing_sections(report) == ["Validation", "Lessons"]
    assert "Validation, Lessons" in continuation_prompt(["Validation", "Lessons"])
    full = "\n".join(f"## {name}\nok" for name in REQUIRED_SECTIONS)
    assert missing_sections(full) == []


def test_assistant_activities_describe_tools_and_text() -> None:
    payload = {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/home/receptionist/repos/www/a.py"},
                },
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "Checking the logs\nmore"},
            ]
        }
    }
    activities = assistant_activities(payload)
    assert activities[0][0] == "Using Read"
    assert activities[0][1].startswith("Reading ")
    assert activities[0][1].endswith("a.py")
    assert activities[1] == ("Using Bash", "Running command")
    assert activities[2] == ("Checking the logs", "Thinking")
    assert assistant_activities({"message": "bad"}) == []


def write_request(directory: Path, **overrides) -> Path:
    created = datetime.now(UTC)
    payload = {
        "id": "8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a",
        "repository_path": str(directory / "repos" / "www"),
        "revision": "a" * 40,
        "command": "systemd-run --collect /bin/true",
        "summary": "Deploy",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(minutes=15)).isoformat(),
    }
    payload.update(overrides)
    path = directory / f"{payload['id']}.json"
    path.write_text(json.dumps(payload))
    return path


def test_deployment_request_validation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    (repo_root / "www").mkdir(parents=True)
    parsed = parse_request_file(write_request(tmp_path), repo_root.resolve())
    assert parsed["revision"] == "a" * 40
    assert parsed["request_id"] == "8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a"
    with pytest.raises(ValueError):
        parse_request_file(
            write_request(tmp_path, revision="main"), repo_root.resolve()
        )
    with pytest.raises(ValueError):
        parse_request_file(
            write_request(tmp_path, repository_path="/etc"),
            repo_root.resolve(),
        )
    created = datetime.now(UTC)
    with pytest.raises(ValueError):
        parse_request_file(
            write_request(
                tmp_path,
                created_at=created.isoformat(),
                expires_at=(created + timedelta(hours=1)).isoformat(),
            ),
            repo_root.resolve(),
        )
    assert is_self_deployment(
        "systemd-run /bin/bash apps/receptionist2/deploy/install-receptionist2"
    )
    assert not is_self_deployment("systemctl restart wnba")


def test_cancel_requeue_and_verbose(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    assert database.request_cancel() is None
    run = accept(database, 10)
    claimed = database.claim_next_run()
    assert claimed["id"] == run["id"]
    cancelled = database.request_cancel()
    assert cancelled["id"] == run["id"]
    assert database.cancel_requested(run["id"])
    database.finish_run(run["id"], status="cancelled", error="Stopped")
    latest = database.latest_unsuccessful_run(1)
    assert latest["id"] == run["id"]
    retry = database.requeue_run(1, run["id"])
    assert retry["id"] != run["id"]
    assert retry["exact_prompt"] == run["exact_prompt"]
    assert retry["status"] == "queued"
    with pytest.raises(LookupError):
        database.requeue_run(999, run["id"])

    assert not database.verbose_for_chat(2)
    database.set_verbose(1, True)
    assert database.verbose_for_chat(2)
    database.set_verbose(1, False)
    assert not database.verbose_for_chat(2)


def test_progress_and_reset(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    run = accept(database, 11)
    database.claim_next_run()
    database.update_run_progress(
        run["id"], activity="Using Bash", current_work="Running command"
    )
    current = database.get_run(run["id"])
    now = datetime.fromisoformat(current["started_at"]) + timedelta(seconds=75)
    text = progress_text(current, verbose=False, now=now)
    assert "Using Bash" in text
    assert "Current: Running command" in text
    assert "Elapsed: 1m 15s" in text

    before = database.active_session(1)
    after = database.reset_session(1, 2)
    assert after["id"] != before["id"]
    assert database.active_session(1)["id"] == after["id"]


def test_pending_rollover_is_single_use(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    session = database.ensure_user(
        1, 2, default_model="claude-opus-5-5", default_effort="high"
    )
    pending = database.create_pending_rollover(
        user_id=1,
        chat_id=2,
        message_id=5,
        session_id=session["id"],
        text="next task",
        reason="context nearly full",
    )
    assert database.get_pending_rollover(1, pending["id"])["exact_text"] == "next task"
    consumed = database.consume_pending_rollover(1, pending["id"])
    assert consumed["id"] == pending["id"]
    with pytest.raises(LookupError):
        database.consume_pending_rollover(1, pending["id"])


def test_deployment_diagnosis_is_idempotent(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    session = database.ensure_user(
        1, 2, default_model="claude-opus-5-5", default_effort="high"
    )
    created = datetime.now(UTC)
    request_id = "8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a"
    assert database.import_deployment_request(
        request_id=request_id,
        user_id=1,
        repository_path="/home/receptionist/repos/www",
        revision="b" * 40,
        command="systemd-run /bin/false",
        summary="Deploy",
        created_at=created.isoformat(),
        expires_at=(created + timedelta(minutes=15)).isoformat(),
    )
    assert database.transition_deployment_request(
        request_id, "pending", "running"
    )
    database.finish_deployment_request(
        request_id, status="failed", exit_code=1, output="boom", error=None
    )
    first, first_created = database.enqueue_deployment_diagnosis(
        request_id=request_id,
        user_id=1,
        chat_id=2,
        session_id=session["id"],
        prompt="diagnose",
    )
    second, second_created = database.enqueue_deployment_diagnosis(
        request_id=request_id,
        user_id=1,
        chat_id=2,
        session_id=session["id"],
        prompt="diagnose",
    )
    assert first_created and not second_created
    assert second["id"] == first["id"]
    assert first["deployment_request_id"] == request_id


def test_gateway_text_helpers() -> None:
    text = session_topics_text(
        "main",
        {
            "recent_topics": [["deploy", "worker"]],
            "context_tokens": 50_000,
            "context_window_tokens": 200_000,
            "successful_runs": 3,
        },
    )
    assert "1. deploy · worker" in text
    assert "(25%)" in text
    waiting = session_topics_text(
        "main",
        {
            "recent_topics": [],
            "context_tokens": None,
            "context_window_tokens": None,
            "successful_runs": 0,
        },
    )
    assert "(none recorded yet)" in waiting
    markup = markup_from_json(
        json.dumps([[{"text": "Retry", "callback_data": "r2:x"}]])
    )
    assert markup.inline_keyboard[0][0].callback_data == "r2:x"
    assert markup_from_json("not json") is None
    assert markup_from_json(None) is None


def fake_proc(tmp_path: Path, swap_free_kb: int = 1048576) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal: 2048000 kB\nMemAvailable: 1024000 kB\n"
        f"SwapTotal: 2097152 kB\nSwapFree: {swap_free_kb} kB\n"
    )
    (proc / "loadavg").write_text("0.42 0.30 0.20 1/100 1234\n")
    process = proc / "4321"
    process.mkdir()
    (process / "stat").write_text("4321 (claude) S 1 4321 4321 0")
    (process / "status").write_text("Name: claude\nVmRSS: 204800 kB\n")
    return proc


def test_resources_from_fake_proc(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path)
    memory = meminfo(proc)
    assert memory == {
        "mem_total_mb": 2000,
        "mem_avail_mb": 1000,
        "swap_total_mb": 2048,
        "swap_used_mb": 1024,
    }
    summary = resource_summary(4321, proc)
    assert "Agent: 200MB RSS" in summary
    assert "load 0.42" in summary
    assert "process metrics unavailable" in resource_summary(None, proc)


def test_memory_monitor_oom_and_sustained_swap() -> None:
    memory = {
        "mem_total_mb": 2000,
        "mem_avail_mb": 100,
        "swap_total_mb": 2048,
        "swap_used_mb": 1500,
    }
    killed = memory_monitor.parse_oom_kills(
        "noise\nOut of memory: Killed process 77 (claude) total-vm:1kB\n"
    )
    assert killed == ["claude"]
    messages, state = memory_monitor.evaluate({}, memory, killed, 1000.0)
    assert len(messages) == 1 and "claude" in messages[0]
    messages, state = memory_monitor.evaluate(state, memory, killed, 2000.0)
    assert messages == []

    state = {}
    alerts = []
    for minute in range(0, 61, 15):
        messages, state = memory_monitor.evaluate(
            state, memory, [], 10_000.0 + minute * 60
        )
        alerts.extend(messages)
    assert len(alerts) == 1 and "swap" in alerts[0]

    low = dict(memory, swap_used_mb=10)
    state = {}
    for minute in range(0, 61, 15):
        messages, state = memory_monitor.evaluate(
            state,
            low if minute == 30 else memory,
            [],
            50_000.0 + minute * 60,
        )
        assert messages == []
    assert memory_monitor.load_state("not json") == {}


def test_maintenance_backs_up_and_prunes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    database = make_database(tmp_path)
    database.enqueue_outbox(2, "hello", kind="test")
    runs = state / "runs"
    runs.mkdir()
    old = runs / "old.receipt.json"
    old.write_text("{}")
    fresh = runs / "new.stdout"
    fresh.write_text("")
    keep = runs / "notes.txt"
    keep.write_text("")
    stale_time = 1_000_000.0
    os.utime(old, (stale_time, stale_time))
    os.utime(keep, (stale_time, stale_time))
    result = run_maintenance(state, now=stale_time + 15 * 86400)
    assert Path(result["backup"]).is_file()
    assert result["run_artifacts_removed"] == 1
    assert not old.exists() and fresh.exists() and keep.exists()
    assert set(result["pruned"]) >= {"outbox", "diagnostic_incidents"}


def test_reauth_runs_outside_sandbox_as_agent() -> None:
    command = reauth_command()
    assert command[0] == "/usr/bin/systemd-run"
    assert "--pipe" in command and "--wait" in command
    assert "--uid=receptionist-agent" in command
    assert command[-3:] == ["/usr/bin/claude", "auth", "login"]


def test_extract_auth_url_strips_ansi_and_wrapping() -> None:
    output = (
        "Browser didn't open? Use the url below:\n\n"
        "\x1b[1mhttps://claude.com/cai/oauth/authorize?code=true&\n"
        "client_id=abc\x1b[0m\n\nPaste code here if prompted > "
    )
    assert (
        extract_auth_url(output)
        == "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    )
    with pytest.raises(RuntimeError):
        extract_auth_url("https://evil.example/x\nPaste code here")
