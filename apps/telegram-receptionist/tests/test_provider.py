import json

from receptionist.providers.base import ProviderResult
from receptionist.providers.claude_cli import build_command, parse_event


def test_build_command_keeps_prompt_as_one_exact_argument() -> None:
    prompt = "yes\nsecond line; $(not-a-shell)"
    command = build_command("/usr/bin/claude", prompt, "session-1", None)
    assert command[command.index("-p") + 1] == prompt
    assert command[-2:] == ["--resume", "session-1"]


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
