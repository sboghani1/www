from __future__ import annotations

import json
from typing import Any

from .base import ProviderResult


def build_command(
    binary: str,
    prompt: str,
    provider_session_id: str | None,
    model: str | None,
    effort: str | None = None,
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
    if effort:
        command.extend(["--effort", effort])
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
        result.current_work = "Preparing response"
        return "result", payload

    if provider_type == "assistant":
        activities = _assistant_activities(payload)
        if activities:
            result.activity, result.current_work = activities[-1]
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


def _assistant_activities(
    payload: dict[str, Any],
) -> list[tuple[str, str]]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    activities: list[tuple[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = str(block.get("name", "tool"))
            tool_input = block.get("input")
            activities.append(
                (
                    f"Using {name}",
                    _tool_current_work(
                        name,
                        tool_input if isinstance(tool_input, dict) else {},
                    ),
                )
            )
        elif block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                first_line = text.splitlines()[0]
                activities.append((first_line[:140], "Thinking"))
    return activities


def _tool_current_work(name: str, tool_input: dict[str, Any]) -> str:
    path = tool_input.get("file_path") or tool_input.get("path")
    compact_path = _compact_path(path) if isinstance(path, str) else ""
    actions = {
        "Read": "Reading",
        "Edit": "Editing",
        "Write": "Writing",
        "Glob": "Finding files in",
        "Grep": "Searching files in",
    }
    if name in actions and compact_path:
        return f"{actions[name]} {compact_path}"
    summaries = {
        "Bash": "Running command",
        "Task": "Delegating task",
        "TodoWrite": "Updating task list",
        "WebSearch": "Researching web",
        "WebFetch": "Reading web page",
        "Skill": "Running skill",
    }
    return summaries.get(name, f"Using {name}")[:100]


def _compact_path(path: str) -> str:
    normalized = path.strip().rstrip("/")
    if not normalized:
        return ""
    marker = "/home/receptionist/repos/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return normalized[-100:]
