import asyncio
import json
from pathlib import Path

import pytest

from decision_helper.claude import (
    ClaudeClient,
    build_command,
    parse_json_object,
    subscription_environment,
)
from helpers import extraction_payload, make_events, write_fake_claude


def test_subscription_environment_strips_api_billing() -> None:
    environment = subscription_environment(
        {
            "ANTHROPIC_API_KEY": "sk-ant-api",
            "ANTHROPIC_AUTH_TOKEN": "x",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
            "HOME": "/home/receptionist-agent",
        }
    )
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert "CLAUDE_CODE_USE_BEDROCK" not in environment
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert environment["HOME"] == "/home/receptionist-agent"


def test_command_is_text_only_without_tools_or_permission_bypass() -> None:
    command = build_command("/usr/bin/claude", "prompt", "claude-opus-5-5", "medium")
    assert command[:3] == ["/usr/bin/claude", "-p", "prompt"]
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command


def test_parse_json_object_tolerates_fences() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


def test_extract_runs_cli_without_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    binary = tmp_path / "claude"
    log = write_fake_claude(binary, extraction_payload())
    client = ClaudeClient(str(binary), "claude-opus-5-5", "medium", 30, tmp_path / "work")

    extraction = asyncio.run(client.extract("Bills plus three", make_events()))

    assert len(extraction.picks) == 3
    call = json.loads(log.read_text().splitlines()[0])
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["cwd"] == str(tmp_path / "work")
    assert "<transcript>\nBills plus three\n</transcript>" in call["argv"][1]


def test_auth_failure_points_to_reauth(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    write_fake_claude(binary, "Failed to authenticate. OAuth token has expired.", exit_code=1)
    client = ClaudeClient(str(binary), "m", "low", 30, tmp_path / "work")
    with pytest.raises(RuntimeError, match="/reauth"):
        asyncio.run(client.extract("x", make_events()))
