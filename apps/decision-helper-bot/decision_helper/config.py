from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


EXPECTED_USERNAME = "decision_helpers_bot"


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


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_id: int
    expected_username: str
    state_dir: Path
    google_credentials: str
    lines_sheet_id: str
    claude_binary: str
    claude_model: str
    claude_effort: str
    claude_timeout_seconds: int
    whisper_model: str
    max_download_bytes: int
    sync_interval_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        allowed_user_id = int(_required("TELEGRAM_ALLOWED_USER_ID"))
        if allowed_user_id <= 0:
            raise ValueError("TELEGRAM_ALLOWED_USER_ID must be positive")
        expected_username = os.getenv(
            "DECISION_HELPER_EXPECTED_USERNAME", EXPECTED_USERNAME
        ).strip().lstrip("@")
        if expected_username != EXPECTED_USERNAME:
            raise ValueError(
                "DECISION_HELPER_EXPECTED_USERNAME must be "
                "decision_helpers_bot"
            )
        state_dir = Path(
            os.getenv(
                "DECISION_HELPER_STATE_DIR",
                "/var/lib/decision-helper-bot",
            )
        )
        if not state_dir.is_absolute():
            raise ValueError("DECISION_HELPER_STATE_DIR must be absolute")
        max_download_bytes = _positive_int(
            "DECISION_HELPER_MAX_DOWNLOAD_BYTES", 20 * 1024 * 1024
        )
        if max_download_bytes > 20 * 1024 * 1024:
            raise ValueError(
                "DECISION_HELPER_MAX_DOWNLOAD_BYTES cannot exceed 20971520"
            )
        effort = os.getenv("DECISION_HELPER_CLAUDE_EFFORT", "medium").lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("DECISION_HELPER_CLAUDE_EFFORT is invalid")
        return cls(
            bot_token=_required("DECISION_HELPER_BOT_TOKEN"),
            allowed_user_id=allowed_user_id,
            expected_username=expected_username,
            state_dir=state_dir,
            google_credentials=_required("GOOGLE_CREDENTIALS"),
            lines_sheet_id=_required("DECISION_HELPER_LINES_SHEET_ID"),
            claude_binary=os.getenv("CLAUDE_BINARY", "/usr/bin/claude"),
            claude_model=os.getenv(
                "DECISION_HELPER_CLAUDE_MODEL", "claude-opus-5-5"
            ),
            claude_effort=effort,
            claude_timeout_seconds=_positive_int(
                "DECISION_HELPER_CLAUDE_TIMEOUT_SECONDS", 300
            ),
            whisper_model=os.getenv(
                "DECISION_HELPER_WHISPER_MODEL", "small.en"
            ),
            max_download_bytes=max_download_bytes,
            sync_interval_seconds=_positive_int(
                "DECISION_HELPER_SYNC_INTERVAL_SECONDS", 300
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "decisions.db"

    @property
    def audio_dir(self) -> Path:
        return self.state_dir / "audio"

    @property
    def model_dir(self) -> Path:
        return self.state_dir / "models"

    @property
    def claude_workdir(self) -> Path:
        return self.state_dir / "claude-workdir"
