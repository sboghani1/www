import asyncio
from types import SimpleNamespace

from receptionist2.watchdog import Watchdog


class Stream:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def read(self, _size: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


class Stdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class Process:
    def __init__(self, returncode: int, stdout: bytes = b"") -> None:
        self.stdin = Stdin()
        self.stdout = Stream(stdout)
        self.stderr = Stream()
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


class Message:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)


def respond(process: Process, helper_results: dict):
    watchdog = Watchdog(SimpleNamespace(helper="helper"), None)
    watchdog._reauth_process = process
    calls = []

    async def helper(action, argument=None, timeout=120):
        calls.append(action)
        return helper_results[action]

    watchdog._helper = helper
    message = Message("the-code#state")
    update = SimpleNamespace(effective_message=message)
    asyncio.run(watchdog.reauth_response(update, None))
    return watchdog, message, calls


def test_reauth_success_restarts_consumers() -> None:
    process = Process(0, b"Claude long-lived token saved.\n")

    watchdog, message, calls = respond(
        process,
        {
            "auth-status": (0, '{"logged_in": true}', ""),
            "apply-auth": (0, '{"restarted": ["decision-helper-bot.service"]}', ""),
        },
    )

    assert process.stdin.written == b"the-code#state\n"
    assert process.stdin.closed and message.deleted
    assert calls == ["auth-status", "apply-auth"]
    assert message.replies[-1].startswith("✅ Claude reauthentication succeeded")
    assert "decision-helper-bot.service" in message.replies[-1]
    assert watchdog._reauth_process is None


def test_reauth_failure_relays_the_session_reason() -> None:
    process = Process(
        1, b"Claude did not issue a token: OAuth error: invalid code\n"
    )

    _, message, calls = respond(process, {})

    assert calls == []
    assert message.replies[-1].startswith(
        "❌ Claude authentication failed: Claude did not issue a token: "
        "OAuth error: invalid code"
    )


def test_reauth_unverified_token_is_reported_as_failure() -> None:
    process = Process(0, b"Claude long-lived token saved.\n")

    _, message, calls = respond(
        process, {"auth-status": (0, '{"logged_in": false}', "")}
    )

    assert calls == ["auth-status"]
    assert message.replies[-1].startswith("❌ Claude is still not authenticated")
