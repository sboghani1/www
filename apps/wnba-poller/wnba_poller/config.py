from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    sheet_id: str
    google_credentials_b64: str
    google_service_account_json: str
    odds_api_key: str
    odds_api_fallback_key: str
    odds_alert_bot_token: str
    odds_alert_chat_id: str
    odds_alert_state_path: Path
    odds_low_remaining: int
    http_timeout_seconds: float
    http_retries: int

    @classmethod
    def from_env(
        cls,
        *,
        require_google: bool = True,
        require_odds: bool = False,
    ) -> "Config":
        load_dotenv()
        sheet_id = os.getenv("WNBA_SHEET_ID", "").strip()
        credentials_b64 = os.getenv("GOOGLE_CREDENTIALS", "").strip()
        credentials_path = os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_JSON", ""
        ).strip()
        odds_api_key = os.getenv("ODDS_API_KEY", "").strip()
        odds_api_fallback_key = os.getenv(
            "ODDS_API_FALLBACK_KEY", ""
        ).strip()
        odds_alert_bot_token = os.getenv(
            "WNBA_ODDS_ALERT_BOT_TOKEN", ""
        ).strip()
        odds_alert_chat_id = os.getenv(
            "WNBA_ODDS_ALERT_CHAT_ID", ""
        ).strip()
        odds_alert_state_path = Path(
            os.getenv(
                "WNBA_ODDS_ALERT_STATE_PATH",
                "/var/lib/wnba-poller/odds_alert_state.json",
            )
        )

        missing: list[str] = []
        if require_google and not sheet_id:
            missing.append("WNBA_SHEET_ID")
        if require_google and not (credentials_b64 or credentials_path):
            missing.append(
                "GOOGLE_CREDENTIALS or GOOGLE_SERVICE_ACCOUNT_JSON"
            )
        if require_odds and not odds_api_key:
            missing.append("ODDS_API_KEY")
        if missing:
            raise RuntimeError(
                "Missing required environment: " + ", ".join(missing)
            )

        timeout = float(os.getenv("WNBA_HTTP_TIMEOUT_SECONDS", "20"))
        retries = int(os.getenv("WNBA_HTTP_RETRIES", "2"))
        low_remaining = int(
            os.getenv("WNBA_ODDS_LOW_REMAINING", "2000")
        )
        if timeout <= 0:
            raise ValueError("WNBA_HTTP_TIMEOUT_SECONDS must be positive")
        if retries < 0 or retries > 5:
            raise ValueError("WNBA_HTTP_RETRIES must be between 0 and 5")
        if low_remaining < 0:
            raise ValueError(
                "WNBA_ODDS_LOW_REMAINING must be non-negative"
            )
        if bool(odds_alert_bot_token) != bool(odds_alert_chat_id):
            raise RuntimeError(
                "WNBA_ODDS_ALERT_BOT_TOKEN and "
                "WNBA_ODDS_ALERT_CHAT_ID must be configured together"
            )

        return cls(
            sheet_id=sheet_id,
            google_credentials_b64=credentials_b64,
            google_service_account_json=credentials_path,
            odds_api_key=odds_api_key,
            odds_api_fallback_key=odds_api_fallback_key,
            odds_alert_bot_token=odds_alert_bot_token,
            odds_alert_chat_id=odds_alert_chat_id,
            odds_alert_state_path=odds_alert_state_path,
            odds_low_remaining=low_remaining,
            http_timeout_seconds=timeout,
            http_retries=retries,
        )
