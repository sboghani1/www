from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL_ALIASES = {
    "opus": "claude-opus-5-5",
    "opus48": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class CommonConfig:
    state_dir: Path
    repo_root: Path
    timezone: ZoneInfo

    @property
    def database_path(self) -> Path:
        return self.state_dir / "receptionist2.db"

    @classmethod
    def from_env(cls) -> "CommonConfig":
        return cls(
            state_dir=Path(
                os.getenv(
                    "RECEPTIONIST2_STATE_DIR",
                    "/var/lib/receptionist2",
                )
            ).resolve(),
            repo_root=Path(
                os.getenv(
                    "RECEPTIONIST2_REPO_ROOT",
                    "/home/receptionist/repos",
                )
            ).resolve(),
            timezone=ZoneInfo(
                os.getenv("RECEPTIONIST2_TIMEZONE", "America/Los_Angeles")
            ),
        )


@dataclass(frozen=True)
class GatewayConfig(CommonConfig):
    telegram_token: str
    allowed_user_id: int
    default_model: str
    default_effort: str
    delivery_poll_seconds: int
    schedule_poll_seconds: int
    nfl_audio_max_bytes: int
    nfl_audio_max_clips: int
    nfl_audio_max_duration_seconds: int

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        common = CommonConfig.from_env()
        effort = os.getenv("RECEPTIONIST2_DEFAULT_EFFORT", DEFAULT_EFFORT).lower()
        if effort not in EFFORT_LEVELS:
            raise ValueError("RECEPTIONIST2_DEFAULT_EFFORT is invalid")
        return cls(
            **common.__dict__,
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            allowed_user_id=int(required("TELEGRAM_ALLOWED_USER_ID")),
            default_model=os.getenv(
                "RECEPTIONIST2_DEFAULT_MODEL", DEFAULT_MODEL
            ),
            default_effort=effort,
            delivery_poll_seconds=positive_int(
                "RECEPTIONIST2_DELIVERY_POLL_SECONDS", 10
            ),
            schedule_poll_seconds=positive_int(
                "RECEPTIONIST2_SCHEDULE_POLL_SECONDS", 30
            ),
            nfl_audio_max_bytes=positive_int(
                "RECEPTIONIST2_NFL_AUDIO_MAX_BYTES", 20 * 1024 * 1024
            ),
            nfl_audio_max_clips=positive_int(
                "RECEPTIONIST2_NFL_AUDIO_MAX_CLIPS", 24
            ),
            nfl_audio_max_duration_seconds=positive_int(
                "RECEPTIONIST2_NFL_AUDIO_MAX_DURATION_SECONDS", 3600
            ),
        )

    @property
    def nfl_audio_dir(self) -> Path:
        return self.state_dir / "nfl-audio"


@dataclass(frozen=True)
class WorkerConfig(CommonConfig):
    claude_binary: str
    agent_launcher: str
    poll_seconds: int
    run_timeout_seconds: int
    run_state_dir: Path
    agent_killer: str = "/usr/local/libexec/receptionist2-agent-killer"
    deploy_request_dir: Path = Path(
        "/home/receptionist/repos/.receptionist/deploy-requests"
    )
    deploy_executor: str = "/usr/local/libexec/receptionist-deploy-executor"
    deploy_timeout_seconds: int = 900
    allowed_user_id: int = 0
    release_link: Path = Path("/opt/receptionist2/current")

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        common = CommonConfig.from_env()
        return cls(
            **common.__dict__,
            claude_binary=os.getenv("CLAUDE_BINARY", "/usr/bin/claude"),
            agent_launcher=os.getenv(
                "RECEPTIONIST2_AGENT_LAUNCHER",
                "/usr/local/libexec/receptionist2-agent-runner",
            ),
            poll_seconds=positive_int("RECEPTIONIST2_WORKER_POLL_SECONDS", 2),
            run_timeout_seconds=positive_int(
                "RECEPTIONIST2_RUN_TIMEOUT_SECONDS", 3600
            ),
            run_state_dir=Path(
                os.getenv(
                    "RECEPTIONIST2_RUN_STATE_DIR",
                    "/var/lib/receptionist2/runs",
                )
            ).resolve(),
            agent_killer=os.getenv(
                "RECEPTIONIST2_AGENT_KILLER",
                "/usr/local/libexec/receptionist2-agent-killer",
            ),
            deploy_request_dir=Path(
                os.getenv(
                    "RECEPTIONIST2_DEPLOY_REQUEST_DIR",
                    "/home/receptionist/repos/.receptionist/deploy-requests",
                )
            ),
            deploy_executor=os.getenv(
                "RECEPTIONIST2_DEPLOY_EXECUTOR",
                "/usr/local/libexec/receptionist-deploy-executor",
            ),
            deploy_timeout_seconds=positive_int(
                "RECEPTIONIST2_DEPLOY_TIMEOUT_SECONDS", 900
            ),
            allowed_user_id=int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0")),
        )


@dataclass(frozen=True)
class WatchdogConfig(CommonConfig):
    telegram_token: str
    allowed_user_id: int
    helper: str
    diagnostic_runner: str

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        common = CommonConfig.from_env()
        return cls(
            **common.__dict__,
            telegram_token=required("WATCHDOG_BOT_TOKEN"),
            allowed_user_id=int(required("WATCHDOG_ALLOWED_USER_ID")),
            helper=os.getenv(
                "RECEPTIONIST2_CONTROL_HELPER",
                "/usr/local/libexec/receptionist2-control",
            ),
            diagnostic_runner=os.getenv(
                "RECEPTIONIST2_DIAGNOSTIC_RUNNER",
                "/usr/local/libexec/receptionist2-diagnostic-runner",
            ),
        )


def resolve_model(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("model is required")
    alias = MODEL_ALIASES.get(normalized.lower())
    if alias:
        return alias
    if not normalized[0].isalnum() or not all(
        char.isalnum() or char in "._-[]" for char in normalized
    ):
        raise ValueError("model ID contains unsupported characters")
    return normalized
