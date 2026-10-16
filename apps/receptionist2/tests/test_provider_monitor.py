from pathlib import Path

import receptionist2.provider_monitor as monitor
from receptionist2.database import Database


def test_blocked_model_must_survive_confirmation() -> None:
    payload = {
        "models": {
            "blocked": {"condition": "blocked", "detail": "first 429"},
            "healthy": {"condition": "ok", "detail": "200"},
        }
    }

    monitor.confirm_model_blocks(
        payload,
        {"models": {"blocked": {"condition": "ok", "detail": "200"}}},
    )

    assert payload["models"]["blocked"]["condition"] == "ok"
    assert payload["models"]["healthy"]["condition"] == "ok"


def test_missing_confirmation_downgrades_block_to_unknown() -> None:
    payload = {
        "models": {
            "blocked": {"condition": "blocked", "detail": "first 429"}
        }
    }

    monitor.confirm_model_blocks(payload, {})

    assert payload["models"]["blocked"]["condition"] == "unknown"


def test_provider_alerts_only_on_failure_transition_and_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    messages: list[str] = []
    monkeypatch.setattr(
        monitor,
        "send",
        lambda _token, _chat_id, text: messages.append(text),
    )

    monitor.update_alert(
        database,
        "token",
        1,
        probe="model:test",
        condition="blocked",
        detail="429",
        failure_condition="blocked",
        failure_message="blocked",
        recovery_message="recovered",
    )
    monitor.update_alert(
        database,
        "token",
        1,
        probe="model:test",
        condition="blocked",
        detail="429 again",
        failure_condition="blocked",
        failure_message="blocked",
        recovery_message="recovered",
    )
    monitor.update_alert(
        database,
        "token",
        1,
        probe="model:test",
        condition="ok",
        detail="200",
        failure_condition="blocked",
        failure_message="blocked",
        recovery_message="recovered",
    )

    assert messages == ["blocked", "recovered"]
