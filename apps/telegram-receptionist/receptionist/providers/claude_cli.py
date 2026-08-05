from __future__ import annotations

import json
from typing import Any

from .base import ProviderResult


def build_command(
    binary: str,
    prompt: str,
    provider_session_id: str | None,
    model: str | None,
) -> list[str]:
    command = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--dangerously-skip-permissions",
    ]
    if provider_session_id:
        command.extend(["--resume", provider_session_id])
    if model:
        command.extend(["--model", model])
    return command


def parse_event(line: str, result: ProviderResult) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = {"raw": line}
        return "unparsed", payload
    if not isinstance(payload, dict):
        return "unparsed", {"raw_json": payload}

    provider_type = str(payload.get("type", "unknown"))
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        result.session_id = session_id

    if provider_type == "result":
        result.is_error = bool(payload.get("is_error"))
        response = payload.get("result")
        if isinstance(response, str):
            result.final_response = response
        result.usage = {
            key: payload[key]
            for key in (
                "duration_ms",
                "duration_api_ms",
                "num_turns",
                "total_cost_usd",
                "usage",
                "modelUsage",
            )
            if key in payload
        }
        result.activity = "Finishing response"
        return "result", payload

    if provider_type == "assistant":
        activities = _assistant_activities(payload)
        if activities:
            result.activity = activities[-1]
        return "assistant", payload

    if provider_type == "system":
        subtype = payload.get("subtype")
        if subtype == "api_retry":
            result.activity = f"API retry {payload.get('attempt', '?')}"
            return "retry", payload
        result.activity = f"Claude system: {subtype or 'initializing'}"
        return "system", payload

    if provider_type == "stream_event":
        return "stream", payload

    return "other", payload


def _assistant_activities(payload: dict[str, Any]) -> list[str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    activities: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "tool")
            activities.append(f"Using {name}")
        elif block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                first_line = text.splitlines()[0]
                activities.append(first_line[:140])
    return activities
