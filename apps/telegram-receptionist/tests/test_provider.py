import json

from receptionist.providers.base import ProviderResult
from receptionist.providers.claude_cli import build_command, parse_event


def test_build_command_keeps_prompt_as_one_exact_argument() -> None:
    prompt = "yes\nsecond line; $(not-a-shell)"
    command = build_command("/usr/bin/claude", prompt, "session-1", None)
    assert command[command.index("-p") + 1] == prompt
    assert command[-2:] == ["--resume", "session-1"]


def test_build_command_sets_model_and_effort() -> None:
    command = build_command(
        "/usr/bin/claude",
        "prompt",
        None,
        "claude-opus-4-8",
        "medium",
    )

    assert command[-4:] == [
        "--model",
        "claude-opus-4-8",
        "--effort",
        "medium",
    ]


def test_parse_result_captures_response_and_session() -> None:
    result = ProviderResult()
    event_type, _ = parse_event(
        json.dumps(
            {
                "type": "result",
                "session_id": "session-2",
                "result": "finished",
                "total_cost_usd": 0.1,
            }
        ),
        result,
    )
    assert event_type == "result"
    assert result.session_id == "session-2"
    assert result.final_response == "finished"


def test_parse_non_object_json_does_not_crash() -> None:
    result = ProviderResult()
    event_type, payload = parse_event('["unexpected"]', result)
    assert event_type == "unparsed"
    assert payload == {"raw_json": ["unexpected"]}


def test_parse_read_activity_includes_compact_file_path() -> None:
    result = ProviderResult()
    parse_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {
                                "file_path": (
                                    "/home/receptionist/repos/www/"
                                    "plans/wnba_poller.plan.txt"
                                )
                            },
                        }
                    ]
                },
            }
        ),
        result,
    )
    assert result.activity == "Using Read"
    assert result.current_work == "Reading www/plans/wnba_poller.plan.txt"


def test_parse_bash_activity_does_not_expose_command() -> None:
    result = ProviderResult()
    parse_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "echo secret-value"},
                        }
                    ]
                },
            }
        ),
        result,
    )
    assert result.current_work == "Running command"
    assert "secret-value" not in result.current_work
