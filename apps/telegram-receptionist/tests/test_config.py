from receptionist.config import Config, TRUSTED_REPO_ROOT


def test_config_uses_single_workspace(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    monkeypatch.setenv("RECEPTIONIST_REPO_ROOT", "/home/receptionist/repos")
    monkeypatch.setenv("RECEPTIONIST_STATE_DIR", "/tmp/receptionist-test-state")

    config = Config.from_env()

    assert len(config.repositories) == 1
    assert config.repositories[0].name == "workspace"
    assert config.repositories[0].path == TRUSTED_REPO_ROOT
