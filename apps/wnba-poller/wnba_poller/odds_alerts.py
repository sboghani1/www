from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx


class OddsAlertNotifier:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        low_remaining: int,
        state_path: Path,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._low_remaining = low_remaining
        self._state_path = state_path
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=20)
        self._now = now

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def primary_unavailable(
        self,
        reason: str,
        *,
        fallback_configured: bool,
    ) -> None:
        if not self.enabled:
            return
        state = self._load_state()
        if state.get("primary_down"):
            return
        fallback = (
            "The free backup key is now being used."
            if fallback_configured
            else "No backup key is configured, so WNBA odds polling stopped."
        )
        if self._send(
            "🛑 WNBA paid Odds API key is unavailable\n\n"
            f"Primary request failed with {reason}. {fallback}"
        ):
            state["primary_down"] = True
            state["primary_down_at"] = self._now()
            self._save_state(state)

    def primary_healthy(
        self,
        *,
        remaining: int | None,
        used: int | None,
    ) -> None:
        if not self.enabled:
            return
        state = self._load_state()
        changed = False
        if state.pop("primary_down", None) is not None:
            state.pop("primary_down_at", None)
            changed = True

        low = remaining is not None and remaining <= self._low_remaining
        if low and not state.get("primary_low"):
            total = (
                str(used + remaining)
                if used is not None
                else "unknown"
            )
            if self._send(
                "📉 WNBA paid Odds API key is running low\n\n"
                f"Remaining {remaining} of {total}; "
                f"alert threshold {self._low_remaining}. "
                "The free key will take over automatically if the paid key "
                "becomes unavailable."
            ):
                state["primary_low"] = True
                state["primary_low_at"] = self._now()
                changed = True
        elif not low and state.pop("primary_low", None) is not None:
            state.pop("primary_low_at", None)
            changed = True

        if changed:
            self._save_state(state)

    def _send(self, text: str) -> bool:
        try:
            response = self._client.post(
                f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(
                f"Odds API alert send failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            return False
        return True

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp"
            )
            temporary.write_text(json.dumps(state, sort_keys=True))
            os.replace(temporary, self._state_path)
        except OSError as exc:
            print(
                f"Odds API alert state write failed: {type(exc).__name__}",
                file=sys.stderr,
            )
