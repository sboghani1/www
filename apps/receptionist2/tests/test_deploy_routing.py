import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from receptionist2.config import WorkerConfig
from receptionist2.database import Database
from receptionist2.deployments import parse_request_file
from receptionist2.worker import Worker

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
USER = 7
DM = 7
GROUP = -1001234567890
RUN_ID = "3f1c2b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b"


def write_request(directory: Path, **overrides) -> Path:
    from datetime import UTC, datetime, timedelta

    created = datetime.now(UTC)
    payload = {
        "id": "8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a",
        "repository_path": str(directory / "repos" / "www"),
        "revision": "a" * 40,
        "command": "systemd-run --collect /bin/true",
        "summary": "Deploy",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(minutes=15)).isoformat(),
        **overrides,
    }
    path = directory / f"{payload['id']}.json"
    path.write_text(json.dumps(payload))
    return path


def load(name: str):
    loader = SourceFileLoader(name.replace("-", "_"), str(DEPLOY / name))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader)
    )
    loader.exec_module(module)
    return module


def test_request_script_records_the_agent_run(tmp_path, monkeypatch) -> None:
    script = load("request-receptionist-deploy")
    repo = tmp_path / "www"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "REQUEST_DIR", tmp_path / "requests")
    outputs = {"status": "", "rev-parse": "b" * 40}

    def run(args, **_kwargs):
        return SimpleNamespace(stdout=outputs.get(args[3], ""))

    monkeypatch.setattr(script.subprocess, "run", run)
    argv = ["x", "--repo", str(repo), "--summary", "s", "--command", "c"]

    for env, expected in ((RUN_ID, RUN_ID), ("not-a-uuid", None)):
        monkeypatch.setenv("RECEPTIONIST2_RUN_ID", env)
        monkeypatch.setattr(sys, "argv", argv)
        script.main()
        [path] = list((tmp_path / "requests").glob("*.json"))
        payload = json.loads(path.read_text())
        assert payload.get("run_id") == expected
        repo_root = tmp_path.resolve()
        assert parse_request_file(path, repo_root)["requester_run_id"] == expected
        path.unlink()


def test_request_files_without_or_with_bad_run_ids_still_parse(tmp_path) -> None:
    repo_root = tmp_path / "repos"
    (repo_root / "www").mkdir(parents=True)
    plain = parse_request_file(write_request(tmp_path), repo_root.resolve())
    assert plain["requester_run_id"] is None
    bad = parse_request_file(write_request(tmp_path, run_id="x"), repo_root.resolve())
    assert bad["requester_run_id"] is None


def test_runner_exports_its_run_id_to_the_agent() -> None:
    source = (DEPLOY / "receptionist2-agent-runner").read_text()
    assert 'os.environ["RECEPTIONIST2_RUN_ID"] = run_id' in source


def make(tmp_path: Path):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    workspace = tmp_path / "repos"
    workspace.mkdir()
    worker = Worker(
        WorkerConfig(
            state_dir=tmp_path,
            repo_root=workspace,
            timezone=ZoneInfo("America/Los_Angeles"),
            claude_binary="/usr/bin/claude",
            agent_launcher="/runner",
            poll_seconds=1,
            run_timeout_seconds=10,
            run_state_dir=tmp_path,
            allowed_user_id=USER,
        ),
        database,
    )
    return database, worker


def test_deploy_notices_go_to_the_requesting_chat(tmp_path: Path) -> None:
    database, worker = make(tmp_path)
    topic = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍊 bait",
        model="m", effort="high",
    )
    run = database.enqueue_run(
        session_id=topic["id"], user_id=USER, chat_id=GROUP, source="message",
        prompt="build it",
    )

    worker._notify("from the topic", request={"requester_run_id": run["id"]})
    worker._notify("legacy request", request={"requester_run_id": None})
    worker._notify("unknown run", request={"requester_run_id": RUN_ID})

    items = [database.claim_outbox() for _ in range(3)]
    assert [(i["telegram_chat_id"], i["telegram_thread_id"]) for i in items] == [
        (GROUP, 11),
        (DM, None),
        (DM, None),
    ]


def test_topic_diagnosis_runs_in_that_topic(tmp_path: Path) -> None:
    database, _ = make(tmp_path)
    topic = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍊 bait",
        model="m", effort="high",
    )
    database.import_deployment_request(
        request_id="8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a",
        user_id=USER,
        repository_path="/r",
        revision="a" * 40,
        command="c",
        summary="s",
        created_at="2026-11-09T00:00:00+00:00",
        expires_at="2026-11-09T00:10:00+00:00",
        requester_run_id=None,
    )
    with database.connect() as connection:
        connection.execute("UPDATE deployment_requests SET status='failed'")
        connection.commit()

    run, created = database.enqueue_deployment_diagnosis(
        request_id="8b0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a",
        user_id=USER,
        chat_id=GROUP,
        session_id=topic["id"],
        prompt="diagnose",
    )
    assert created and run["telegram_thread_id"] == 11
