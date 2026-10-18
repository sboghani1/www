from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


EXPECTED_USERNAME = "clipboard2_bot"


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_id: int
    expected_username: str
    state_dir: Path
    max_download_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("CLIPBOARD_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("CLIPBOARD_BOT_TOKEN is required")
        user_id = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))
        if user_id <= 0:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_ID must be positive")
        expected = os.getenv(
            "CLIPBOARD_EXPECTED_USERNAME", EXPECTED_USERNAME
        ).strip().lstrip("@")
        if expected != EXPECTED_USERNAME:
            raise ValueError(
                "CLIPBOARD_EXPECTED_USERNAME must be clipboard2_bot"
            )
        state_dir = Path(
            os.getenv(
                "CLIPBOARD_STATE_DIR",
                "/var/lib/telegram-clipboard",
            )
        )
        if not state_dir.is_absolute():
            raise ValueError("CLIPBOARD_STATE_DIR must be absolute")
        max_bytes = int(
            os.getenv("CLIPBOARD_MAX_DOWNLOAD_BYTES", str(20 * 1024 * 1024))
        )
        if max_bytes <= 0 or max_bytes > 20 * 1024 * 1024:
            raise ValueError(
                "CLIPBOARD_MAX_DOWNLOAD_BYTES must be between 1 and 20971520"
            )
        return cls(
            bot_token=token,
            allowed_user_id=user_id,
            expected_username=expected,
            state_dir=state_dir,
            max_download_bytes=max_bytes,
        )
