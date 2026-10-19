import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from receptionist2.config import WorkerConfig
from receptionist2.database import Database
from receptionist2.worker import Worker


def test_worker_recovers_completed_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "repos"
    workspace.mkdir()
    state = tmp_path / "state"
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    database = Database(state / "db.sqlite")
    database.initialize()
    session = database.ensure_user(
        1, 2, default_model="model", default_effort="max"
    )
    run, _ = database.accept_message(
        user_id=1,
        chat_id=2,
        message_id=3,
        text="prompt",
        default_model=session["model"],
        default_effort=session["effort"],
    )
    assert database.claim_next_run()
    (receipts / f"{run['id']}.receipt.json").write_text(
        json.dumps(
            {
                "final_response": "recovered",
                "session_id": "provider",
                "is_error": False,
                "usage": {},
            }
        )
    )
    worker = Worker(
        WorkerConfig(
            state_dir=state,
            repo_root=workspace,
            timezone=ZoneInfo("America/Los_Angeles"),
            claude_binary="/usr/bin/claude",
            agent_launcher="/runner",
            poll_seconds=1,
            run_timeout_seconds=10,
            run_state_dir=receipts,
        ),
        database,
    )

    asyncio.run(worker.reconcile())

    assert database.get_run(run["id"])["status"] == "succeeded"
    outbox = database.claim_outbox()
    assert outbox and outbox["body"] == "recovered"


def make_worker(tmp_path: Path, **overrides) -> tuple[Worker, Database]:
    workspace = tmp_path / "repos"
    (workspace / "www").mkdir(parents=True, exist_ok=True)
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    database = Database(tmp_path / "state" / "db.sqlite")
    database.initialize()
    config = WorkerConfig(
        state_dir=tmp_path / "state",
        repo_root=workspace.resolve(),
        timezone=ZoneInfo("America/Los_Angeles"),
        claude_binary="/usr/bin/claude",
        agent_launcher="/runner",
        poll_seconds=1,
        run_timeout_seconds=10,
        run_state_dir=receipts,
        allowed_user_id=1,
        **overrides,
    )
    return Worker(config, database), database


def test_incomplete_investigation_queues_one_continuation(
    tmp_path: Path,
) -> None:
    worker, database = make_worker(tmp_path)
    session = database.ensure_user(1, 2, default_model="m", default_effort="high")
    run = database.enqueue_run(
        session_id=session["id"],
        user_id=1,
        chat_id=2,
        source="investigation",
        prompt="investigate",
    )
    assert database.claim_next_run()["id"] == run["id"]
    database.finish_run(run["id"], status="succeeded", final_response="x")
    worker._continue_investigation(run, "## Findings\nonly this")
    continuation = database.claim_next_run()
    assert continuation["id"] != run["id"]
    assert continuation["continuation_of"] == run["id"]
    assert "Evidence" in continuation["exact_prompt"]

    worker._continue_investigation(continuation, "still incomplete")
    database.finish_run(continuation["id"], status="succeeded")
    assert database.claim_next_run() is None


def test_deployment_requests_are_ingested_and_removed(tmp_path: Path) -> None:
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    worker, database = make_worker(tmp_path, deploy_request_dir=request_dir)
    database.ensure_user(1, 2, default_model="m", default_effort="high")
    created = datetime.now(UTC)
    request_id = "1f0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a"
    (request_dir / f"{request_id}.json").write_text(
        json.dumps(
            {
                "id": request_id,
                "repository_path": str(tmp_path / "repos" / "www"),
                "revision": "c" * 40,
                "command": "systemd-run /bin/true",
                "summary": "Deploy",
                "created_at": created.isoformat(),
                "expires_at": (created + timedelta(minutes=10)).isoformat(),
            }
        )
    )
    (request_dir / "bogus.json").write_text("{}")
    worker.ingest_deployment_requests()
    assert list(request_dir.iterdir()) == []
    pending = database.pending_deployment_requests()
    assert [request["id"] for request in pending] == [request_id]


def test_worker_reports_finished_self_deployment(tmp_path: Path) -> None:
    worker, database = make_worker(
        tmp_path, release_link=tmp_path / "missing-release"
    )
    database.ensure_user(1, 2, default_model="m", default_effort="high")
    database.set_state("deploy_drain", "abcdef12-0000@123")
    worker.finish_self_deployment()
    assert database.get_state("deploy_drain", "") == ""
    message = database.claim_outbox()
    assert message and "abcdef12" in message["body"]


def test_worker_closes_self_deployment_at_requested_revision(
    tmp_path: Path,
) -> None:
    revision = "c" * 40
    release = tmp_path / "releases" / revision
    release.mkdir(parents=True)
    release_link = tmp_path / "current"
    release_link.symlink_to(release)
    worker, database = make_worker(tmp_path, release_link=release_link)
    database.ensure_user(1, 2, default_model="m", default_effort="high")
    request_id = "1f0e8a3c-6f7e-4a59-9d0c-1f2e3d4c5b6a"
    created = datetime.now(UTC)
    database.import_deployment_request(
        request_id=request_id,
        user_id=1,
        repository_path=str(tmp_path),
        revision=revision,
        command="install-receptionist2 --restart",
        summary="Deploy",
        created_at=created.isoformat(),
        expires_at=(created + timedelta(minutes=10)).isoformat(),
    )
    assert database.transition_deployment_request(
        request_id, "pending", "approved"
    )
    assert database.transition_deployment_request(
        request_id, "approved", "running"
    )
    database.set_state("deploy_drain", f"{request_id}@123")

    worker.finish_self_deployment()

    request = database.get_deployment_request(request_id)
    assert request["status"] == "succeeded"
    assert request["exit_code"] == 0
    assert revision in request["output"]
    assert database.get_state("deploy_drain", "") == ""
