from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .session_policy import usage_context_tokens

WORKSPACE_MARKER = "/home/receptionist/repos/"


@dataclass
class ProviderResult:
    session_id: str | None = None
    final_response: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    activity: str = "Starting agent"
    current_work: str = "Preparing Claude"
    context_tokens: int | None = None
    context_window_tokens: int | None = None


def build_command(
    binary: str,
    prompt: str,
    model: str,
    effort: str,
    provider_session_id: str | None,
) -> list[str]:
    command = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        model,
        "--effort",
        effort,
    ]
    if provider_session_id:
        command.extend(["--resume", provider_session_id])
    return command


def consume_event(line: str, result: ProviderResult) -> bool:
    """Apply one stream-json event. Returns True when activity changed."""
    payload = json.loads(line)
    if not isinstance(payload, dict):
        return False
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        result.session_id = session_id
    event_type = payload.get("type")
    if event_type == "assistant":
        message = payload.get("message")
        if isinstance(message, dict):
            tokens, window = usage_context_tokens(message.get("usage"))
            if tokens is not None:
                result.context_tokens = tokens
            if window is not None:
                result.context_window_tokens = window
        activities = assistant_activities(payload)
        if activities:
            result.activity, result.current_work = activities[-1]
            return True
        return False
    if event_type == "system":
        subtype = payload.get("subtype")
        if subtype == "api_retry":
            result.activity = f"API retry {payload.get('attempt', '?')}"
        else:
            result.activity = f"Claude system: {subtype or 'initializing'}"
        return True
    if event_type != "result":
        return False
    result.is_error = bool(payload.get("is_error"))
    response = payload.get("result")
    if isinstance(response, str):
        result.final_response = response.strip()
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
    _, window = usage_context_tokens(payload.get("modelUsage"))
    if window is not None:
        result.context_window_tokens = window
    result.activity = "Finishing response"
    result.current_work = "Preparing response"
    return True


def assistant_activities(payload: dict[str, Any]) -> list[tuple[str, str]]:
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
                    tool_current_work(
                        name,
                        tool_input if isinstance(tool_input, dict) else {},
                    ),
                )
            )
        elif block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                activities.append((text.splitlines()[0][:140], "Thinking"))
    return activities


def tool_current_work(name: str, tool_input: dict[str, Any]) -> str:
    path = tool_input.get("file_path") or tool_input.get("path")
    compact = compact_path(path) if isinstance(path, str) else ""
    actions = {
        "Read": "Reading",
        "Edit": "Editing",
        "Write": "Writing",
        "Glob": "Finding files in",
        "Grep": "Searching files in",
    }
    if name in actions and compact:
        return f"{actions[name]} {compact}"
    summaries = {
        "Bash": "Running command",
        "Task": "Delegating task",
        "TodoWrite": "Updating task list",
        "WebSearch": "Researching web",
        "WebFetch": "Reading web page",
        "Skill": "Running skill",
    }
    return summaries.get(name, f"Using {name}")[:100]


def compact_path(path: str) -> str:
    normalized = path.strip().rstrip("/")
    if WORKSPACE_MARKER in normalized:
        normalized = normalized.split(WORKSPACE_MARKER, 1)[1]
    return normalized[-100:]


def provider_error_message(result: ProviderResult) -> str:
    if not result.is_error or not result.final_response:
        return ""
    normalized = result.final_response.lower()
    if "failed to authenticate" in normalized or (
        "oauth" in normalized and "expired" in normalized
    ):
        return (
            "Claude authentication expired. Send /reauth to the watchdog "
            "bot to log in again."
        )
    if any(
        phrase in normalized
        for phrase in ("hit your session limit", "usage limit", "rate limit")
    ):
        return f"Claude usage limit reached. {result.final_response.strip()}"
    return ""
