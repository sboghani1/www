import subprocess

from receptionist2 import control, diagnostic


def run_diagnosis(monkeypatch, tmp_path, auth_env: str | None):
    path = tmp_path / "auth.env"
    if auth_env is not None:
        path.write_text(auth_env)
    monkeypatch.setattr(control, "AUTH_ENV", path)
    monkeypatch.setattr(diagnostic, "status", lambda _database: {})
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "report", "")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)
    assert diagnostic.diagnose(None, tmp_path, "incident") == "report"
    return captured


def test_diagnostic_agent_gets_auth_env_token_through_environment(
    monkeypatch, tmp_path
) -> None:
    token = "sk-ant-oat01-secret"
    captured = run_diagnosis(
        monkeypatch, tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={token}\n"
    )

    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert captured["argv"][:3] == [
        "/usr/bin/sudo",
        "-n",
        "--preserve-env=CLAUDE_CODE_OAUTH_TOKEN",
    ]
    assert not any(token in argument for argument in captured["argv"])


def test_diagnostic_agent_without_auth_env_is_unchanged(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    captured = run_diagnosis(monkeypatch, tmp_path, None)

    assert captured["argv"][:3] == ["/usr/bin/sudo", "-n", "-H"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]
