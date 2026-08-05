import pytest

from wnba_poller.config import Config


def test_config_validates_required_names_without_exposing_values(
    monkeypatch,
) -> None:
    for name in (
        "WNBA_SHEET_ID",
        "GOOGLE_CREDENTIALS",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "ODDS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as error:
        Config.from_env(require_google=True, require_odds=True)

    message = str(error.value)
    assert "WNBA_SHEET_ID" in message
    assert "ODDS_API_KEY" in message


def test_config_accepts_service_account_path(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "/secure/creds.json")
    monkeypatch.setenv("ODDS_API_KEY", "secret")

    config = Config.from_env(require_google=True, require_odds=True)

    assert config.sheet_id == "sheet"
    assert config.google_service_account_json == "/secure/creds.json"
