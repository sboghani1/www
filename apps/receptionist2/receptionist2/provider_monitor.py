from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from . import memory_monitor
from .config import CommonConfig, required
from .database import Database
from .resources import meminfo

CONFIRM_DELAY_SECONDS = 20


def send(token: str, chat_id: int, text: str) -> None:
    data = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    urllib.request.urlopen(request, timeout=20).read()


def probe(helper: str) -> tuple[dict, str, str]:
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", helper, "provider"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        return (
            {},
            "unknown",
            result.stderr.strip()[-500:] or "provider probe failed",
        )
    payload = json.loads(result.stdout)
    return (
        payload,
        str(payload.get("condition") or "unknown"),
        str(payload.get("detail") or ""),
    )


def confirm_model_blocks(
    payload: dict,
    confirmation: dict,
) -> None:
    for model, model_status in list(payload.get("models", {}).items()):
        if model_status.get("condition") != "blocked":
            continue
        payload["models"][model] = confirmation.get("models", {}).get(
            model,
            {
                "condition": "unknown",
                "detail": "Blocked result was not confirmed by the reprobe",
            },
        )


def check_memory(database: Database, token: str, chat_id: int) -> None:
    now = time.time()
    state = memory_monitor.load_state(
        database.get_state(memory_monitor.STATE_KEY, "")
    )
    killed = memory_monitor.scan_oom(float(state.get("last_oom_check", now)))
    messages, state = memory_monitor.evaluate(state, meminfo(), killed, now)
    for message in messages:
        send(token, chat_id, message)
    database.set_state(memory_monitor.STATE_KEY, json.dumps(state))


def main() -> None:
    config = CommonConfig.from_env()
    database = Database(config.database_path)
    database.initialize()
    token = required("WATCHDOG_BOT_TOKEN")
    chat_id = int(required("WATCHDOG_ALLOWED_USER_ID"))
    try:
        check_memory(database, token, chat_id)
    except Exception as error:
        print(f"memory check failed: {error!r}")
    helper = os.getenv(
        "RECEPTIONIST2_CONTROL_HELPER",
        "/usr/local/libexec/receptionist2-control",
    )
    payload, condition, detail = probe(helper)
    blocked_models = {
        model
        for model, model_status in payload.get("models", {}).items()
        if model_status.get("condition") == "blocked"
    }
    if blocked_models:
        time.sleep(CONFIRM_DELAY_SECONDS)
        confirmation, _, _ = probe(helper)
        confirm_model_blocks(payload, confirmation)
    update_alert(
        database,
        token,
        chat_id,
        probe="auth",
        condition=condition,
        detail=detail,
        failure_condition="auth_failed",
        failure_message=(
            f"🔑 Receptionist2 provider authentication failed: {detail}"
        ),
        recovery_message="✅ Receptionist2 provider authentication recovered.",
    )
    for model, model_status in payload.get("models", {}).items():
        model_condition = str(model_status.get("condition") or "unknown")
        model_detail = str(model_status.get("detail") or "")
        update_alert(
            database,
            token,
            chat_id,
            probe=f"model:{model}",
            condition=model_condition,
            detail=model_detail,
            failure_condition="blocked",
            failure_message=(
                f"🚫 Receptionist2 model {model} is blocked: {model_detail}"
            ),
            recovery_message=f"✅ Receptionist2 model {model} is serving again.",
        )


def update_alert(
    database: Database,
    token: str,
    chat_id: int,
    *,
    probe: str,
    condition: str,
    detail: str,
    failure_condition: str,
    failure_message: str,
    recovery_message: str,
) -> None:
    previous = database.provider_alert(probe)
    changed = previous is None or previous["condition"] != condition
    alert = condition == failure_condition and changed
    recovered = (
        condition == "ok"
        and previous is not None
        and previous["condition"] == failure_condition
    )
    alerted_at = previous.get("alerted_at") if previous else None
    if alert or recovered:
        send(token, chat_id, failure_message if alert else recovery_message)
        alerted_at = datetime.now(UTC).isoformat()
    database.set_provider_alert(
        probe, condition, detail, alerted_at=alerted_at
    )


if __name__ == "__main__":
    main()
