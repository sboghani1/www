import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from receptionist.watchdog import (
    ReceptionistWatchdog,
    WatchdogConfig,
    _format_status,
    watchdog_menu,
)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def test_unauthorized_user_cannot_run_watchdog_command() -> None:
    watchdog = ReceptionistWatchdog(WatchdogConfig("token", 123))
    handler = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=999, type="private"),
    )

    asyncio.run(watchdog.authorized(handler)(update, SimpleNamespace()))

    handler.assert_not_awaited()


def test_restart_uses_only_fixed_helper_action() -> None:
    watchdog = ReceptionistWatchdog(WatchdogConfig("token", 123))
    watchdog._run_helper = AsyncMock(
        return_value=(
            0,
            json.dumps(
                {
                    "active_state": "active",
                    "sub_state": "running",
                    "main_pid": "42",
                    "restarts": "0",
                    "revision": "a" * 40,
                    "deployment_drain": False,
                }
            ),
            "",
        )
    )
    message = FakeMessage()
    update = SimpleNamespace(effective_message=message)

    asyncio.run(watchdog.restart(update, SimpleNamespace()))

    watchdog._run_helper.assert_awaited_once_with("restart", timeout=60)
    assert "Restart completed" in message.replies[-1]
    assert "/recover" in message.replies[-1]


def test_watchdog_menu_exposes_buttons() -> None:
    markup = watchdog_menu()
    labels = [
        button.text for row in markup.inline_keyboard for button in row
    ]

    assert labels == [
        "Status",
        "Memory",
        "Logs",
        "Ping",
        "Restart receptionist",
    ]


def test_restart_button_requires_confirmation() -> None:
    watchdog = ReceptionistWatchdog(WatchdogConfig("token", 123))
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        answer=AsyncMock(),
        data="watchdog:restart",
        message=message,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=message,
    )

    asyncio.run(watchdog.button(update, SimpleNamespace()))

    query.answer.assert_awaited_once()
    reply_markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert (
        reply_markup.inline_keyboard[0][0].callback_data
        == "watchdog:restart-confirm"
    )


def test_status_format_includes_revision_and_drain() -> None:
    text = _format_status(
        json.dumps(
            {
                "active_state": "active",
                "sub_state": "running",
                "main_pid": "42",
                "restarts": "1",
                "revision": "1234567890abcdef",
                "deployment_drain": True,
            }
        )
    )

    assert "active (running)" in text
    assert "1234567890ab" in text
    assert "Deployment drain: yes" in text
