from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TRUSTED_REPO_ROOT = Path("/home/receptionist/repos").resolve()
TRUSTED_CLAUDE_BINARY = "/usr/bin/claude"
TRUSTED_WNBA_HELPER = "/usr/local/libexec/receptionist-wnba-helper"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_CLAUDE_EFFORT = "medium"
CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


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
    wnba_helper: str
    wnba_helper_timeout_seconds: int
    agent_timeout_seconds: int
    max_queued_messages: int
    model: str | None
    effort: str | None = None

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
        wnba_helper = os.getenv(
            "WNBA_HELPER", TRUSTED_WNBA_HELPER
        )
        if wnba_helper != TRUSTED_WNBA_HELPER:
            raise ValueError(
                "WNBA_HELPER must match the fixed privileged launcher"
            )
        wnba_timeout = _positive_int(
            "WNBA_HELPER_TIMEOUT_SECONDS", 45
        )
        if wnba_timeout > 120:
            raise ValueError(
                "WNBA_HELPER_TIMEOUT_SECONDS must not exceed 120"
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
            wnba_helper=wnba_helper,
            wnba_helper_timeout_seconds=wnba_timeout,
            agent_timeout_seconds=timeout,
            max_queued_messages=queue_limit,
            model=os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL) or None,
            effort=_optional_choice(
                "CLAUDE_EFFORT",
                DEFAULT_CLAUDE_EFFORT,
                CLAUDE_EFFORT_LEVELS,
            ),
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


def _optional_choice(
    name: str, default: str, choices: set[str]
) -> str | None:
    value = os.getenv(name, default).strip().lower()
    if not value:
        return None
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {allowed}")
    return value
