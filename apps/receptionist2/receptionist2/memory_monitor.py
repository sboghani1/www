from __future__ import annotations

import json
import subprocess
from typing import Any

SWAP_ALERT_MB = 1024
SWAP_SUSTAINED_SECONDS = 40 * 60
SWAP_MIN_SAMPLES = 4
OOM_DEBOUNCE_SECONDS = 60 * 60
SWAP_DEBOUNCE_SECONDS = 24 * 60 * 60
HISTORY_KEEP = 12
STATE_KEY = "memory_monitor"


def scan_oom(since_epoch: float) -> list[str]:
    try:
        output = subprocess.run(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/journalctl",
                "-k",
                "-S",
                f"@{int(since_epoch)}",
                "-o",
                "cat",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_oom_kills(output)


def parse_oom_kills(output: str) -> list[str]:
    killed = []
    for line in output.splitlines():
        if "Out of memory: Killed process" not in line:
            continue
        name = "process"
        if "(" in line and ")" in line:
            name = line[line.rfind("(") + 1 : line.rfind(")")]
        killed.append(name)
    return killed


def load_state(raw: str) -> dict[str, Any]:
    try:
        state = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def debounced(
    last_alert: dict[str, Any], key: str, now: float, interval: float
) -> bool:
    previous = last_alert.get(key)
    return previous is None or now - float(previous) >= interval


def evaluate(
    state: dict[str, Any],
    memory: dict[str, int],
    killed: list[str],
    now: float,
) -> tuple[list[str], dict[str, Any]]:
    messages: list[str] = []
    last_alert = dict(state.get("last_alert", {}))
    summary = (
        f"RAM {memory['mem_avail_mb']}MB free of {memory['mem_total_mb']}MB, "
        f"swap {memory['swap_used_mb']}/{memory['swap_total_mb']}MB used."
    )
    if killed and debounced(last_alert, "oom", now, OOM_DEBOUNCE_SECONDS):
        messages.append(
            "🔴 VPS out of memory — kernel OOM-killed: "
            f"{', '.join(sorted(set(killed)))}\n{summary}\n"
            "A terminated service may need /restart or /diagnose."
        )
        last_alert["oom"] = now

    history = [
        sample
        for sample in state.get("swap_history", [])
        if isinstance(sample, list) and len(sample) == 2
    ]
    history.append([now, memory["swap_used_mb"]])
    history = history[-HISTORY_KEEP:]
    window = [
        sample
        for sample in history
        if now - sample[0] <= SWAP_SUSTAINED_SECONDS + 900
    ]
    sustained = (
        len(window) >= SWAP_MIN_SAMPLES
        and now - window[0][0] >= SWAP_SUSTAINED_SECONDS
        and all(value >= SWAP_ALERT_MB for _, value in window)
    )
    if sustained and debounced(last_alert, "swap", now, SWAP_DEBOUNCE_SECONDS):
        messages.append(
            "🟡 VPS swap under sustained pressure for 40+ minutes.\n"
            f"{summary}\n"
            "Not an outage, but the host is consistently over its RAM budget."
        )
        last_alert["swap"] = now
    return messages, {
        "last_oom_check": now,
        "swap_history": history,
        "last_alert": last_alert,
    }
