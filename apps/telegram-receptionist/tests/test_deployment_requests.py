import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from receptionist.bot import Receptionist
from receptionist.config import Config, RepositoryConfig
from receptionist.database import Database


def test_request_file_is_copied_into_database_and_removed(tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    repository = repo_root / "www"
    request_dir = repo_root / ".receptionist" / "deploy-requests"
    repository.mkdir(parents=True)
    (repository / ".git").mkdir()
    request_dir.mkdir(parents=True)
    request_id = "44444444-4444-4444-8444-444444444444"
    now = datetime.now(UTC)
    request_path = request_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "repository_path": str(repository),
                "revision": "d" * 40,
                "summary": "Deploy immutable request",
                "command": "echo exact-command",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    config = Config(
        telegram_token="test-token",
        allowed_user_id=123,
        repo_root=repo_root,
        repositories=(RepositoryConfig("workspace", repo_root),),
        state_dir=tmp_path / "state",
        claude_binary="/usr/bin/claude",
        agent_launcher="/launcher",
        agent_killer="/killer",
        deploy_request_dir=request_dir,
        deploy_executor="/executor",
        deploy_timeout_seconds=900,
        agent_timeout_seconds=3600,
        max_queued_messages=10,
        model=None,
    )
    database = Database(config.database_path)
    database.initialize(config.repositories)
    receptionist = Receptionist(config, database)

    asyncio.run(receptionist._ingest_deployment_requests())

    assert not request_path.exists()
    request = database.find_deployment_request(123, request_id, ("pending",))
    assert request["command"] == "echo exact-command"
    assert request["revision"] == "d" * 40
