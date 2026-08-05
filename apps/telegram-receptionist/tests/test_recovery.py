import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError

import receptionist.runner as runner_module
from receptionist.config import RepositoryConfig
from receptionist.database import Database
from receptionist.runner import AgentRunner
from receptionist.runner import next_update_label


class FakeBot:
    def __init__(self) -> None:
        self.fail_on_call: int | None = 2
        self.calls = 0
        self.sent: list[str] = []

    async def edit_message_text(self, **kwargs) -> None:
        return None

    async def send_message(self, chat_id: int, text: str) -> None:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise NetworkError("temporary delivery failure")
        self.sent.append(text)

    async def send_document(self, *args, **kwargs) -> None:
        raise AssertionError("test response should use text chunks")


class BlockingBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.fail_on_call = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, chat_id: int, text: str) -> None:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.sent.append(text)


class MissingProcess:
    pid = 999_999_999
    returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


def _finished_run(tmp_path: Path) -> tuple[Database, dict]:
    repository = tmp_path / "repos"
    repository.mkdir()
    database = Database(tmp_path / "state" / "receptionist.db")
    database.initialize((RepositoryConfig("workspace", repository),))
    database.ensure_user_state(123, 456)
    session = database.create_session(123)
    run = database.enqueue_run(session["id"], 456, "prompt")
    database.start_run(run["id"], 321)
    database.finish_run(
        run["id"],
        status="succeeded",
        exit_code=0,
        final_response="a" * 5_000,
        error=None,
    )
    return database, database.get_run(run["id"])


def test_delivery_retry_resumes_after_last_successful_chunk(
    tmp_path: Path,
) -> None:
    database, run = _finished_run(tmp_path)
    bot = FakeBot()
    runner = AgentRunner(SimpleNamespace(), database, bot)

    assert not asyncio.run(runner._deliver_run(run))
    failed = database.get_run(run["id"])
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_cursor"] == 1
    assert len(bot.sent) == 1

    bot.fail_on_call = None
    assert asyncio.run(runner._deliver_run(failed))
    delivered = database.get_run(run["id"])
    assert delivered["delivery_status"] == "delivered"
    assert delivered["delivery_cursor"] == 2
    assert len(bot.sent) == 2


def test_concurrent_delivery_attempts_send_response_once(
    tmp_path: Path,
) -> None:
    database, run = _finished_run(tmp_path)
    bot = BlockingBot()
    runner = AgentRunner(SimpleNamespace(), database, bot)

    async def exercise() -> tuple[bool, bool]:
        first = asyncio.create_task(runner._deliver_run(run))
        await bot.started.wait()
        second = asyncio.create_task(runner._deliver_run(run))
        await asyncio.sleep(0)
        bot.release.set()
        return await first, await second

    first, second = asyncio.run(exercise())
    assert first
    assert not second
    assert len(bot.sent) == 2
    delivered = database.get_run(run["id"])
    assert delivered["delivery_status"] == "delivered"
    assert delivered["delivery_attempts"] == 1


def test_missing_process_is_detected_without_waiting_for_timeout(
    tmp_path: Path,
) -> None:
    database, _ = _finished_run(tmp_path)
    runner = AgentRunner(SimpleNamespace(), database, FakeBot())

    disappeared = asyncio.run(
        runner._wait_for_process(
            MissingProcess(),
            timeout=10,
            poll_interval=0.01,
        )
    )

    assert disappeared


def test_recovery_requires_final_text_only_assistant_record() -> None:
    assert AgentRunner._recovery_is_complete(
        {
            "last_conversation_type": "assistant",
            "last_assistant_has_tool_use": False,
            "final_response_timestamp": "2026-08-05T20:00:00+00:00",
            "last_conversation_timestamp": "2026-08-05T20:00:00+00:00",
            "task_statuses": ["completed", "pending"],
        }
    )
    assert not AgentRunner._recovery_is_complete(
        {
            "last_conversation_type": "assistant",
            "last_assistant_has_tool_use": True,
            "final_response_timestamp": "2026-08-05T20:00:00+00:00",
            "last_conversation_timestamp": "2026-08-05T20:00:00+00:00",
        }
    )


def test_recovery_response_must_belong_to_current_run() -> None:
    started = datetime.now(UTC)
    run = {"started_at": started.isoformat()}
    complete = {
        "last_conversation_type": "assistant",
        "last_assistant_has_tool_use": False,
    }

    assert not AgentRunner._recovery_matches_run(
        {
            **complete,
            "final_response_timestamp": (
                started - timedelta(seconds=1)
            ).isoformat(),
            "last_conversation_timestamp": (
                started - timedelta(seconds=1)
            ).isoformat(),
        },
        run,
    )
    assert not AgentRunner._recovery_matches_run(
        {
            **complete,
            "last_conversation_type": "user",
            "final_response_timestamp": (
                started + timedelta(seconds=1)
            ).isoformat(),
            "last_conversation_timestamp": (
                started + timedelta(seconds=1)
            ).isoformat(),
        },
        run,
    )
    assert AgentRunner._recovery_matches_run(
        {
            **complete,
            "final_response_timestamp": (
                started + timedelta(seconds=1)
            ).isoformat(),
            "last_conversation_timestamp": (
                started + timedelta(seconds=1)
            ).isoformat(),
        },
        run,
    )


def test_restarted_worker_does_not_run_queue_while_orphan_is_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database, active = _finished_run(tmp_path)
    with database._connect() as connection:
        connection.execute(
            """
            UPDATE runs
            SET status='running', process_id=?, finished_at=NULL,
                delivery_status='none'
            WHERE id=?
            """,
            (987_654, active["id"]),
        )
        session_id = connection.execute(
            "SELECT session_id FROM runs WHERE id=?", (active["id"],)
        ).fetchone()["session_id"]
        connection.commit()
    queued = database.enqueue_run(session_id, 456, "next prompt")
    runner = AgentRunner(SimpleNamespace(), database, FakeBot())
    monkeypatch.setattr(runner_module, "process_group_alive", lambda _: True)

    async def exercise() -> None:
        runner.start()
        await asyncio.sleep(0.05)
        assert database.get_run(queued["id"])["status"] == "queued"
        await runner.close()

    asyncio.run(exercise())


def test_next_update_rounds_now_plus_30_seconds_up_to_minute() -> None:
    now = datetime(2026, 8, 5, 20, 43, 27, tzinfo=UTC)
    assert next_update_label(now) == "4:44 PM ET"


def test_failed_run_can_recover_missing_result_event(tmp_path: Path) -> None:
    database, run = _finished_run(tmp_path)
    database.update_provider_session(run["session_id"], "session-1")
    database.finish_run(
        run["id"],
        status="failed",
        exit_code=0,
        final_response=None,
        error="Claude exited without a final response.",
    )
    failed = database.get_run(run["id"])
    response_time = (
        datetime.fromisoformat(failed["started_at"]) + timedelta(seconds=1)
    ).isoformat()
    bot = FakeBot()
    bot.fail_on_call = None
    runner = AgentRunner(SimpleNamespace(), database, bot)
    runner._recover_run_receipt = AsyncMock(return_value={})
    runner._recover_provider_session = AsyncMock(
        return_value={
            "final_response": "recovered response",
            "final_response_timestamp": response_time,
            "last_conversation_type": "assistant",
            "last_conversation_timestamp": response_time,
            "last_assistant_has_tool_use": False,
        }
    )

    message = asyncio.run(runner._recover_failed_run(failed))

    recovered = database.get_run(run["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["delivery_status"] == "delivered"
    assert bot.sent == ["recovered response"]
    assert "prompt was not replayed" in message


def test_failed_run_prefers_durable_launcher_receipt(tmp_path: Path) -> None:
    database, run = _finished_run(tmp_path)
    database.finish_run(
        run["id"],
        status="failed",
        exit_code=None,
        final_response=None,
        error="Claude exited without a final response.",
    )
    failed = database.get_run(run["id"])
    bot = FakeBot()
    bot.fail_on_call = None
    runner = AgentRunner(SimpleNamespace(), database, bot)
    runner._recover_run_receipt = AsyncMock(
        return_value={
            "exit_code": 0,
            "final_response": "receipt response",
        }
    )
    runner._recover_provider_session = AsyncMock(return_value={})

    message = asyncio.run(runner._recover_failed_run(failed))

    recovered = database.get_run(run["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["delivery_status"] == "delivered"
    assert bot.sent == ["receipt response"]
    assert "launcher receipt" in message
    runner._recover_provider_session.assert_not_awaited()
