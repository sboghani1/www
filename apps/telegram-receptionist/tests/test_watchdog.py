import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from receptionist.watchdog import (
    ReceptionistWatchdog,
    WatchdogConfig,
    _extract_auth_url,
    _format_logs,
    _format_status,
    watchdog_menu,
)


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []
        self.deleted = False

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)

    async def delete(self) -> None:
        self.deleted = True


class FakeAuthStdin:
    def __init__(self, process) -> None:
        self.process = process
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.returncode = 0
        self.process.stdout.feed_eof()
        self.process.stderr.feed_eof()
        self.process.finished.set()


class FakeAuthProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(
            b"Visit: https://claude.com/cai/oauth/authorize?state=test\n"
            b"Paste code here if prompted >"
        )
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.finished = asyncio.Event()
        self.stdin = FakeAuthStdin(self)

    async def wait(self) -> int:
        await self.finished.wait()
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self.stdin.close()

    def kill(self) -> None:
        self.stdin.close()


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
        "Reauthenticate Claude",
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
                "claude_logged_in": False,
            }
        )
    )

    assert "active (running)" in text
    assert "Claude provider: authentication required" in text
    assert "1234567890ab" in text
    assert "Deployment drain: yes" in text


def test_extract_auth_url_handles_terminal_wrapping() -> None:
    assert _extract_auth_url(
        "If the browser didn't open, visit: "
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc\r\n"
        "&state=def\r\nPaste code here if prompted >"
    ) == (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
        "&state=def"
    )


def test_reauthentication_button_requires_confirmation() -> None:
    watchdog = ReceptionistWatchdog(WatchdogConfig("token", 123))
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        answer=AsyncMock(),
        data="watchdog:reauth",
        message=message,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=message,
    )

    asyncio.run(watchdog.button(update, SimpleNamespace()))

    reply_markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert (
        reply_markup.inline_keyboard[0][0].callback_data
        == "watchdog:reauth-confirm"
    )


def test_reauthentication_relays_and_deletes_one_time_response(
    monkeypatch,
) -> None:
    async def exercise() -> tuple[FakeAuthProcess, FakeMessage, FakeMessage]:
        process = FakeAuthProcess()

        async def create_process(*args, **kwargs):
            return process

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", create_process
        )
        watchdog = ReceptionistWatchdog(WatchdogConfig("token", 123))
        watchdog._run_helper = AsyncMock(
            return_value=(
                0,
                json.dumps({"claude_logged_in": True}),
                "",
            )
        )
        start_message = FakeMessage()
        start_update = SimpleNamespace(
            effective_message=start_message,
            effective_chat=SimpleNamespace(id=123),
        )
        await watchdog.start_reauthentication(
            start_update, SimpleNamespace()
        )

        response_message = FakeMessage("one-time-code")
        response_update = SimpleNamespace(effective_message=response_message)
        await watchdog.reauthentication_response(
            response_update, SimpleNamespace()
        )
        return process, start_message, response_message

    process, start_message, response_message = asyncio.run(exercise())

    assert "https://claude.com/" in start_message.replies[-1]
    assert process.stdin.data == b"one-time-code\n"
    assert response_message.deleted
    assert "reauthentication succeeded" in response_message.replies[-1]
    assert all(
        "one-time-code" not in reply
        for reply in start_message.replies + response_message.replies
    )


def test_logs_hide_scheduler_noise_and_format_meaningful_entries() -> None:
    text = _format_logs(
        json.dumps(
            {
                "entries": [
                    {
                        "timestamp": "1785976137000000",
                        "priority": "6",
                        "message": (
                            "2026-08-05 20:28:57,960 INFO "
                            "apscheduler.executors.default: Job executed successfully"
                        ),
                    },
                    {
                        "timestamp": "1785976140000000",
                        "priority": "4",
                        "message": (
                            "2026-08-05 20:29:00,000 WARNING receptionist: "
                            "Agent worker is not alive"
                        ),
                    },
                ]
            }
        )
    )

    assert "Recent receptionist activity" in text
    assert "⚠️ Agent worker is not alive" in text
    assert "Job executed successfully" not in text
    assert "1 routine scheduler entries hidden" in text
