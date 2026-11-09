import io
import subprocess
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


def isolate_auth_files(monkeypatch, tmp_path, auth_env: str | None = None):
    path = tmp_path / "auth.env"
    if auth_env is not None:
        path.write_text(auth_env)
    monkeypatch.setattr(control, "AUTH_ENV", path)
    monkeypatch.setattr(control, "CREDENTIALS", tmp_path / "credentials.json")


def test_logged_out_cli_status_is_an_auth_failure(monkeypatch, tmp_path) -> None:
    isolate_auth_files(monkeypatch, tmp_path)

    def logged_out(argv, timeout=60):
        # `claude auth status` exits 1 when logged out but still prints JSON.
        return subprocess.CompletedProcess(
            argv, 1, stdout='{"loggedIn": false, "authMethod": "none"}', stderr=""
        )

    monkeypatch.setattr(control, "run", logged_out)

    status = control.provider_status()

    assert status["condition"] == "auth_failed"
    assert status["logged_in"] is False
    assert "logged out" in status["detail"]
    assert "/reauth" in status["detail"]


def test_auth_env_token_takes_precedence_over_cli_login(
    monkeypatch, tmp_path
) -> None:
    isolate_auth_files(
        monkeypatch,
        tmp_path,
        "OTHER=1\nCLAUDE_CODE_OAUTH_TOKEN=old\n"
        "export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-new'\n",
    )
    probed = []

    def probe(token, model):
        probed.append((token, model))
        return {"condition": "ok", "detail": "HTTP 200"}

    def unexpected_cli(*_args, **_kwargs):
        raise AssertionError("the CLI login must not decide token auth")

    monkeypatch.setattr(control, "probe_model", probe)
    monkeypatch.setattr(control, "run", unexpected_cli)

    assert control.auth_status("claude-test") == {
        "logged_in": True,
        "auth_method": "oauth_token",
        "detail": "HTTP 200",
    }
    assert probed == [("sk-ant-oat01-new", "claude-test")]


def test_rejected_auth_env_token_is_an_auth_failure(monkeypatch, tmp_path) -> None:
    isolate_auth_files(monkeypatch, tmp_path, "CLAUDE_CODE_OAUTH_TOKEN=dead\n")
    monkeypatch.setattr(
        control,
        "probe_model",
        lambda *_: {"condition": "auth_failed", "detail": "HTTP 401: expired"},
    )

    status = control.provider_status()

    assert status["condition"] == "auth_failed"
    assert "was rejected (HTTP 401: expired)" in status["detail"]



def test_apply_auth_restarts_only_active_consumers(monkeypatch) -> None:
    calls = []

    def fake_run(state):
        def run(argv, timeout=60):
            calls.append(argv[1:])
            code = state if argv[1] == "is-active" else 0
            return subprocess.CompletedProcess(argv, code, "", "")

        return run

    monkeypatch.setattr(control, "run", fake_run(0))
    assert control.apply_auth() == {"restarted": ["decision-helper-bot.service"]}
    assert calls[-1] == ["restart", "decision-helper-bot.service"]

    calls.clear()
    monkeypatch.setattr(control, "run", fake_run(3))
    assert control.apply_auth() == {"restarted": []}
    assert all(call[0] == "is-active" for call in calls)
