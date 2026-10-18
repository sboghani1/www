from pathlib import Path


def test_deploy_uses_python_312_and_installs_helpers() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (root / "deploy/install-telegram-clipboard").read_text()
    service = (root / "deploy/telegram-clipboard.service").read_text()
    configure = (root / "deploy/configure-telegram-clipboard").read_text()

    assert "/usr/local/bin/python3.12" in installer
    assert '"$python_binary" -m venv "$release/.venv"' in installer
    assert "/usr/local/bin/clipboard-send" in installer
    assert "/usr/local/bin/clipboard-latest" in installer
    assert "User=receptionist-agent" in service
    assert "Group=receptionist2-state" in service
    assert "StateDirectory=telegram-clipboard" in service
    assert "clipboard2_bot" in configure
