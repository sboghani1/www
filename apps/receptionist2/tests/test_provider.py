from receptionist2.config import resolve_model
from receptionist2.provider import ProviderResult, build_command, consume_event


def test_model_alias_and_command_are_explicit() -> None:
    model = resolve_model("opus")
    command = build_command(
        "/usr/bin/claude", "prompt", model, "max", "session-id"
    )

    assert model == "claude-opus-5-5"
    assert resolve_model("opus48") == "claude-opus-4-8"
    assert command[-2:] == ["--resume", "session-id"]
    assert command[command.index("--effort") + 1] == "max"


def test_result_event_records_response_session_and_usage() -> None:
    result = ProviderResult()
    consume_event(
        '{"type":"result","session_id":"abc","result":"done",'
        '"total_cost_usd":0.2}',
        result,
    )

    assert result.session_id == "abc"
    assert result.final_response == "done"
    assert result.usage["total_cost_usd"] == 0.2


def test_default_model_and_effort() -> None:
    from receptionist2.config import DEFAULT_EFFORT, DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-opus-5-5"
    assert DEFAULT_EFFORT == "high"
