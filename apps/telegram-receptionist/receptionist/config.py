from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TRUSTED_REPO_ROOT = Path("/home/receptionist/repos").resolve()
TRUSTED_CLAUDE_BINARY = "/usr/bin/claude"


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_id: int
    repo_root: Path
    repositories: tuple[RepositoryConfig, ...]
    state_dir: Path
    claude_binary: str
    agent_launcher: str
    agent_killer: str
    deploy_request_dir: Path
    deploy_executor: str
    deploy_timeout_seconds: int
    agent_timeout_seconds: int
    max_queued_messages: int
    model: str | None

    @property
    def database_path(self) -> Path:
        return self.state_dir / "receptionist.db"

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat"

    @classmethod
    def from_env(cls) -> "Config":
        token = _required("TELEGRAM_BOT_TOKEN")
        allowed_user_id = int(_required("TELEGRAM_ALLOWED_USER_ID"))
        repo_root = Path(
            os.getenv("RECEPTIONIST_REPO_ROOT", "/home/receptionist/repos")
        ).expanduser().resolve()
        if repo_root != TRUSTED_REPO_ROOT:
            raise ValueError(
                f"RECEPTIONIST_REPO_ROOT must match the privileged launcher: "
                f"{TRUSTED_REPO_ROOT}"
            )
        repositories = (RepositoryConfig(name="workspace", path=repo_root),)
        state_dir = Path(
            os.getenv("RECEPTIONIST_STATE_DIR", "/var/lib/telegram-receptionist")
        ).expanduser().resolve()
        timeout = _positive_int("AGENT_TIMEOUT_SECONDS", 3600)
        queue_limit = _positive_int("MAX_QUEUED_MESSAGES", 10)
        claude_binary = os.getenv("CLAUDE_BINARY", "/usr/bin/claude")
        if claude_binary != TRUSTED_CLAUDE_BINARY:
            raise ValueError(
                f"CLAUDE_BINARY must match the privileged launcher: "
                f"{TRUSTED_CLAUDE_BINARY}"
            )
        return cls(
            telegram_token=token,
            allowed_user_id=allowed_user_id,
            repo_root=repo_root,
            repositories=repositories,
            state_dir=state_dir,
            claude_binary=claude_binary,
            agent_launcher=os.getenv(
                "AGENT_LAUNCHER", "/usr/local/libexec/receptionist-agent-runner"
            ),
            agent_killer=os.getenv(
                "AGENT_KILLER", "/usr/local/libexec/receptionist-agent-killer"
            ),
            deploy_request_dir=Path(
                os.getenv(
                    "DEPLOY_REQUEST_DIR",
                    "/home/receptionist/repos/.receptionist/deploy-requests",
                )
            ).resolve(),
            deploy_executor=os.getenv(
                "DEPLOY_EXECUTOR",
                "/usr/local/libexec/receptionist-deploy-executor",
            ),
            deploy_timeout_seconds=_positive_int("DEPLOY_TIMEOUT_SECONDS", 900),
            agent_timeout_seconds=timeout,
            max_queued_messages=queue_limit,
            model=os.getenv("CLAUDE_MODEL") or None,
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
