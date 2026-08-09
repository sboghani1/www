import json

from wnba_poller.odds_alerts import OddsAlertNotifier


class _Response:
    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def post(self, url: str, json: dict) -> _Response:
        self.messages.append(json["text"])
        return _Response()


def _notifier(tmp_path, client: _Client) -> OddsAlertNotifier:
    return OddsAlertNotifier(
        bot_token="receptionist-token",
        chat_id="123",
        low_remaining=2000,
        state_path=tmp_path / "state.json",
        client=client,
        now=lambda: 1234,
    )


def test_primary_down_alert_is_sent_once(tmp_path) -> None:
    client = _Client()
    notifier = _notifier(tmp_path, client)

    notifier.primary_unavailable(
        "HTTP 401", fallback_configured=True
    )
    notifier.primary_unavailable(
        "HTTP 401", fallback_configured=True
    )

    assert len(client.messages) == 1
    assert "free backup key is now being used" in client.messages[0]
    assert json.loads((tmp_path / "state.json").read_text())[
        "primary_down"
    ]


def test_low_alert_rearms_after_quota_recovers(tmp_path) -> None:
    client = _Client()
    notifier = _notifier(tmp_path, client)

    notifier.primary_healthy(remaining=1999, used=8001)
    notifier.primary_healthy(remaining=1900, used=8100)
    notifier.primary_healthy(remaining=9000, used=1000)
    notifier.primary_healthy(remaining=1800, used=8200)

    assert len(client.messages) == 2
    assert all("running low" in message for message in client.messages)


def test_state_write_failure_does_not_raise(tmp_path) -> None:
    client = _Client()
    state_path = tmp_path / "directory"
    state_path.mkdir()
    notifier = OddsAlertNotifier(
        bot_token="receptionist-token",
        chat_id="123",
        low_remaining=2000,
        state_path=state_path,
        client=client,
    )

    notifier.primary_unavailable(
        "HTTP 401", fallback_configured=True
    )

    assert len(client.messages) == 1
