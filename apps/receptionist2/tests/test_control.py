import io
import time
import urllib.error

from receptionist2 import control
from receptionist2.database import Database


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_probe_model_sends_oauth_bearer_token(monkeypatch) -> None:
    captured = {}

    def open_request(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(control.urllib.request, "urlopen", open_request)

    result = control.probe_model("secret-token", "claude-test")

    assert result["condition"] == "ok"
    assert captured == {
        "authorization": "Bearer secret-token",
        "timeout": 30,
    }


def test_probe_model_classifies_rate_limit(monkeypatch) -> None:
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages",
        429,
        "rate limited",
        {},
        io.BytesIO(b'{"error":{"message":"limit reached"}}'),
    )

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(control.urllib.request, "urlopen", raise_error)

    result = control.probe_model("token", "claude-test")

    assert result == {
        "condition": "blocked",
        "detail": "HTTP 429: limit reached",
    }


def test_restart_waits_for_application_readiness(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    request = database.create_restart_request(1, 2, 0)
    calls = {"unit_status": 0, "sleep": 0}

    monkeypatch.setattr(
        control,
        "run",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 0, "stderr": ""}
        )(),
    )

    def unit_status(_unit):
        calls["unit_status"] += 1
        if calls["unit_status"] == 4:
            database.set_state("gateway_status", "ready")
            database.set_state("worker_status", "ready")
            database.set_state("worker_heartbeat", str(time.time()))
        return {
            "active": "active",
            "sub": "running",
            "pid": "1",
            "restarts": "0",
        }

    monkeypatch.setattr(control, "unit_status", unit_status)
    monkeypatch.setattr(
        control.time,
        "sleep",
        lambda _seconds: calls.__setitem__("sleep", calls["sleep"] + 1),
    )

    control.restart(database, request["id"])

    assert calls["unit_status"] == 4
    assert calls["sleep"] == 1
    assert database.get_restart_request(request["id"])["status"] == "succeeded"
