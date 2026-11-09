import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "receptionist2-agent-runner"


def load_runner():
    loader = SourceFileLoader("receptionist2_agent_runner", str(RUNNER))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader)
    )
    loader.exec_module(module)
    return module


def clear_token(monkeypatch) -> None:
    # setenv first so teardown restores the variable's original absence.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")


def test_runner_exports_token_from_auth_env(monkeypatch, tmp_path) -> None:
    runner = load_runner()
    auth_env = tmp_path / "auth.env"
    auth_env.write_text(
        "# long-lived token\nOTHER=1\nCLAUDE_CODE_OAUTH_TOKEN=old\n"
        'export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-new"\n'
    )
    monkeypatch.setattr(runner, "CLAUDE_AUTH_ENV", auth_env)
    clear_token(monkeypatch)

    runner.load_claude_token()

    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-new"
    assert "OTHER" not in os.environ


def test_runner_without_auth_env_keeps_credentials_login(
    monkeypatch, tmp_path
) -> None:
    runner = load_runner()
    monkeypatch.setattr(runner, "CLAUDE_AUTH_ENV", tmp_path / "missing.env")
    clear_token(monkeypatch)

    runner.load_claude_token()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
