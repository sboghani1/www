from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psutil
from telegram import Bot
from telegram.error import BadRequest, RetryAfter, TelegramError

from .config import Config
from .database import Database
from .formatting import ATTACHMENT_THRESHOLD, split_message, text_attachment
from .providers.base import ProviderResult
from .providers.claude_cli import build_command, parse_event

log = logging.getLogger("receptionist.runner")


class AgentRunner:
    def __init__(self, config: Config, database: Database, bot: Bot) -> None:
        self.config = config
        self.database = database
        self.bot = bot
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active_process: asyncio.subprocess.Process | None = None
        self._active_run_id: str | None = None
        self._cancel_requested = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work_loop())
        self._wake.set()

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self.cancel_active()

    def notify(self) -> None:
        self._wake.set()

    @property
    def healthy(self) -> bool:
        worker_healthy = self._worker is not None and not self._worker.done()
        process = self._active_process
        process_healthy = (
            process is None
            or process.returncode is not None
            or process_group_alive(process.pid)
        )
        return worker_healthy and process_healthy

    async def recover_startup(self) -> list[str]:
        messages = []
        for run in self.database.running_runs():
            messages.append(await self._recover_run(run))
        await self.retry_pending_deliveries()
        return messages

    async def recover(self) -> str:
        active = self.database.active_run()
        if active and active.get("process_id"):
            process_id = int(active["process_id"])
            if process_group_alive(process_id):
                return (
                    f"Run {active['id'][:8]} is still running as PID "
                    f"{process_id}; no restart was performed."
                )
        if active:
            await self._stop_stale_worker()
            message = await self._recover_run(self.database.get_run(active["id"]))
        else:
            message = "No stale active run was found."
        delivered = await self.retry_pending_deliveries()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work_loop())
        self._wake.set()
        if delivered:
            message += f" Retried {delivered} pending Telegram delivery item(s)."
        return message

    async def retry_pending_deliveries(self) -> int:
        delivered = 0
        for run in self.database.pending_delivery_runs():
            if await self._deliver_run(run):
                delivered += 1
        return delivered

    async def cancel_active(self) -> bool:
        process = self._active_process
        if process is None or process.returncode is not None:
            return False
        self._cancel_requested = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            await self._force_kill_process_group(process.pid)
        return True

    async def _stop_stale_worker(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        self._active_process = None
        self._active_run_id = None
        self._cancel_requested = False

    async def _work_loop(self) -> None:
        while True:
            active = self.database.active_run()
            if active:
                await self._recover_run(active)
                if self.database.active_run():
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=5)
                    except TimeoutError:
                        pass
                    continue
            run = self.database.next_queued_run()
            if run is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._execute(run)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.exception("Run %s crashed outside normal handling", run["id"])
                await self.cancel_active()
                current = self.database.get_run(run["id"])
                if current["status"] in {"queued", "running"}:
                    self.database.finish_run(
                        run["id"],
                        status="failed",
                        exit_code=None,
                        final_response=None,
                        error=f"Receptionist worker error: {type(error).__name__}",
                    )
                try:
                    await self.bot.send_message(
                        run["telegram_chat_id"],
                        f"❌ Run {run['id'][:8]} failed inside the receptionist.",
                    )
                except TelegramError:
                    pass
                self._active_process = None
                self._active_run_id = None
                self._cancel_requested = False

    async def _execute(self, run: dict[str, Any]) -> None:
        repository = Path(run["repository_path"]).resolve()
        if not repository.is_relative_to(self.config.repo_root):
            self.database.finish_run(
                run["id"],
                status="failed",
                exit_code=None,
                final_response=None,
                error="Repository path escaped the configured root.",
            )
            return
        if not repository.is_dir():
            await self._fail_before_start(run, "Repository directory is missing.")
            return

        claude_command = build_command(
            self.config.claude_binary,
            run["exact_prompt"],
            run["provider_session_id"],
            self.config.model,
        )
        status_message = await self.bot.send_message(
            run["telegram_chat_id"],
            f"▶️ Run {run['id'][:8]} started in {run['repository_name']}.\n"
            "Claude is starting…",
        )
        self.database.set_status_message(run["id"], status_message.message_id)

        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/sudo",
                "-n",
                "-H",
                "-u",
                "receptionist-agent",
                self.config.agent_launcher,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            await self._fail_before_start(run, f"Could not start Claude: {error}")
            return

        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "repository": str(repository),
                    "command": claude_command,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
        await process.stdin.drain()
        process.stdin.close()

        self._active_process = process
        self._active_run_id = run["id"]
        self._cancel_requested = False
        self.database.start_run(run["id"], process.pid)
        run = self.database.get_run(run["id"])
        result = ProviderResult()
        stderr_lines: list[str] = []
        started = time.monotonic()
        state = self.database.get_user_state(run["telegram_user_id"])

        stdout_task = asyncio.create_task(
            self._read_stdout(run, process, result)
        )
        stderr_task = asyncio.create_task(
            self._read_stderr(process, stderr_lines)
        )
        status_task = asyncio.create_task(
            self._status_loop(
                run,
                process,
                result,
                status_message.message_id,
                started,
                bool(state["verbose"]),
            )
        )

        timed_out = False
        process_disappeared = False
        try:
            process_disappeared = await self._wait_for_process(
                process, self.config.agent_timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                await self._force_kill_process_group(process.pid)
                await process.wait()
        finally:
            await self._finish_stream_tasks(
                stdout_task, stderr_task, cancel=process_disappeared
            )
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)

        if result.session_id:
            self.database.update_provider_session(
                run["session_id"], result.session_id
            )

        recovered = False
        if process_disappeared and result.session_id and not result.final_response:
            recovery = await self._recover_provider_session(result.session_id)
            recovered_response = recovery.get("final_response")
            if (
                self._recovery_matches_run(recovery, run)
                and isinstance(recovered_response, str)
                and recovered_response.strip()
            ):
                result.final_response = recovered_response.strip()
                recovered = True

        stderr = "\n".join(stderr_lines[-30:]).strip()
        if self._cancel_requested:
            status = "cancelled"
            error = "Cancelled from Telegram."
        elif timed_out:
            status = "timed_out"
            error = (
                f"Run exceeded {self.config.agent_timeout_seconds // 60} minutes."
            )
        elif (
            (process.returncode == 0 or recovered)
            and result.final_response
            and not result.is_error
        ):
            status = "succeeded"
            error = None
        else:
            status = "failed"
            error = stderr or (
                "Claude's process disappeared without a recoverable final response."
                if process_disappeared
                else "Claude exited without a final response."
            )

        self.database.finish_run(
            run["id"],
            status=status,
            exit_code=0 if recovered else process.returncode,
            final_response=result.final_response or None,
            error=error,
            usage=result.usage,
        )
        await self._deliver_run(self.database.get_run(run["id"]))
        self._active_process = None
        self._active_run_id = None
        self._cancel_requested = False

    async def _wait_for_process(
        self,
        process: asyncio.subprocess.Process,
        timeout: int,
        poll_interval: float = 5,
    ) -> bool:
        wait_task = asyncio.create_task(process.wait())
        deadline = time.monotonic() + timeout
        missing_checks = 0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait(
                    {wait_task}, timeout=min(poll_interval, remaining)
                )
                if done:
                    await wait_task
                    return False
                if process_group_alive(process.pid):
                    missing_checks = 0
                    continue
                missing_checks += 1
                if missing_checks >= 2:
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)
                    return True
        except asyncio.CancelledError:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            raise

    @staticmethod
    async def _finish_stream_tasks(
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
        *,
        cancel: bool,
    ) -> None:
        tasks = (stdout_task, stderr_task)
        if not cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=10
                )
                return
            except TimeoutError:
                pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover_provider_session(
        self, session_id: str
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            self.config.agent_launcher,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps(
            {"action": "recover_session", "session_id": session_id}
        ).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=30
            )
        except TimeoutError:
            process.terminate()
            await process.wait()
            return {}
        if process.returncode != 0:
            log.warning(
                "Claude session recovery failed for %s: %s",
                session_id,
                stderr.decode("utf-8", errors="replace")[-500:],
            )
            return {}
        try:
            recovered = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return recovered if isinstance(recovered, dict) else {}

    async def _recover_run(self, run: dict[str, Any]) -> str:
        process_id = run.get("process_id")
        if process_id and process_group_alive(int(process_id)):
            return (
                f"Run {run['id'][:8]} still has a live agent process group; "
                "left it running."
            )
        session_id = run.get("provider_session_id")
        recovery = (
            await self._recover_provider_session(session_id)
            if isinstance(session_id, str) and session_id
            else {}
        )
        response = recovery.get("final_response")
        if (
            self._recovery_matches_run(recovery, run)
            and isinstance(response, str)
            and response.strip()
        ):
            self.database.finish_run(
                run["id"],
                status="succeeded",
                exit_code=0,
                final_response=response.strip(),
                error=None,
            )
            message = (
                f"Recovered completed run {run['id'][:8]} from Claude's "
                "durable session log."
            )
        else:
            self.database.finish_run(
                run["id"],
                status="failed",
                exit_code=None,
                final_response=None,
                error=(
                    "Claude stopped without a recoverable final response; "
                    "the run was not replayed."
                ),
            )
            message = (
                f"Run {run['id'][:8]} had no live process or recoverable "
                "final response and was marked failed without replay."
            )
        await self._deliver_run(self.database.get_run(run["id"]))
        return message

    @staticmethod
    def _recovery_is_complete(recovery: dict[str, Any]) -> bool:
        return (
            recovery.get("last_conversation_type") == "assistant"
            and not recovery.get("last_assistant_has_tool_use", True)
            and recovery.get("final_response_timestamp")
            == recovery.get("last_conversation_timestamp")
        )

    @classmethod
    def _recovery_matches_run(
        cls,
        recovery: dict[str, Any],
        run: dict[str, Any],
    ) -> bool:
        if not cls._recovery_is_complete(recovery):
            return False
        response_timestamp = recovery.get("final_response_timestamp")
        started_at = run.get("started_at")
        if not isinstance(response_timestamp, str) or not response_timestamp:
            return False
        if not isinstance(started_at, str) or not started_at:
            return False
        try:
            response_time = datetime.fromisoformat(
                response_timestamp.replace("Z", "+00:00")
            )
            run_start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return response_time >= run_start

    async def _read_stdout(
        self,
        run: dict[str, Any],
        process: asyncio.subprocess.Process,
        result: ProviderResult,
    ) -> None:
        assert process.stdout is not None
        sequence = 0
        while line_bytes := await process.stdout.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            normalized_type, payload = parse_event(line, result)
            provider_type = str(payload.get("type", "unparsed"))
            self.database.add_event(
                run["id"],
                sequence,
                provider_type,
                normalized_type,
                payload,
            )
            sequence += 1
            if result.session_id:
                self.database.update_provider_session(
                    run["session_id"], result.session_id
                )

    async def _read_stderr(
        self, process: asyncio.subprocess.Process, lines: list[str]
    ) -> None:
        assert process.stderr is not None
        while line_bytes := await process.stderr.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            if line:
                lines.append(line)

    async def _status_loop(
        self,
        run: dict[str, Any],
        process: asyncio.subprocess.Process,
        result: ProviderResult,
        message_id: int,
        started: float,
        verbose: bool,
    ) -> None:
        interval = 30 if verbose else 45
        while process.returncode is None:
            await asyncio.sleep(interval)
            elapsed = int(time.monotonic() - started)
            text = (
                f"⏳ Run {run['id'][:8]} · {run['repository_name']}\n"
                f"{result.activity}\n"
                f"Current: {result.current_work}\n"
                f"Elapsed: {elapsed // 60}m {elapsed % 60}s\n"
                f"Next update by: {next_update_label()}"
            )
            if verbose:
                text += "\n" + resource_summary(process.pid)
            await self._safe_edit(run["telegram_chat_id"], message_id, text)

    async def _deliver_run(self, run: dict[str, Any]) -> bool:
        try:
            await self._finish_telegram(run)
        except TelegramError as error:
            self.database.mark_delivery_failed(
                run["id"], f"{type(error).__name__}: {error}"
            )
            return False
        self.database.mark_delivery_succeeded(run["id"])
        return True

    async def _finish_telegram(self, run: dict[str, Any]) -> None:
        status = str(run["status"])
        response = str(run.get("final_response") or "")
        error = str(run.get("error") or "")
        message_id = run.get("status_message_id")
        icons = {
            "succeeded": "✅",
            "failed": "❌",
            "cancelled": "🛑",
            "timed_out": "⌛",
        }
        if message_id:
            await self._safe_edit(
                run["telegram_chat_id"],
                int(message_id),
                f"{icons.get(status, '•')} Run {run['id'][:8]} {status}.",
            )
        if status != "succeeded":
            messages: list[tuple[str, Any]] = [
                ("text", error or "The run did not complete.")
            ]
        elif len(response) > ATTACHMENT_THRESHOLD:
            messages = [
                (
                    "document",
                    text_attachment(response, f"run-{run['id'][:8]}.txt"),
                )
            ]
        else:
            messages = [("text", chunk) for chunk in split_message(response)]

        cursor = int(run.get("delivery_cursor") or 0)
        for index, (kind, payload) in enumerate(messages[cursor:], start=cursor):
            if kind == "document":
                await self.bot.send_document(
                    run["telegram_chat_id"],
                    payload,
                    caption="The complete agent response is attached.",
                )
            else:
                await self.bot.send_message(run["telegram_chat_id"], payload)
            self.database.set_delivery_cursor(run["id"], index + 1)

    async def _fail_before_start(self, run: dict[str, Any], error: str) -> None:
        self.database.finish_run(
            run["id"],
            status="failed",
            exit_code=None,
            final_response=None,
            error=error,
        )
        await self.bot.send_message(run["telegram_chat_id"], f"❌ {error}")

    async def _safe_edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id
            )
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise
        except RetryAfter as error:
            await asyncio.sleep(float(error.retry_after))
        except TelegramError:
            return

    async def _force_kill_process_group(self, process_group_id: int) -> None:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            self.config.agent_killer,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"process_group_id": process_group_id}).encode("utf-8")
        )
        await process.stdin.drain()
        process.stdin.close()
        await process.wait()


def resource_summary(process_id: int) -> str:
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = psutil.cpu_percent(interval=None)
    try:
        process = psutil.Process(process_id)
        processes = [process, *process.children(recursive=True)]
        rss = sum(item.memory_info().rss for item in processes) / (1024 * 1024)
        process_cpu = sum(item.cpu_percent(interval=None) for item in processes)
        process_line = f"Agent: {rss:.0f}MB RSS · {process_cpu:.0f}% CPU"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process_line = "Agent: process metrics unavailable"
    return (
        f"{process_line}\n"
        f"VPS: {virtual.percent:.0f}% RAM · {swap.percent:.0f}% swap · "
        f"{cpu:.0f}% CPU"
    )


def process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def next_update_label(now: datetime | None = None) -> str:
    eastern = ZoneInfo("America/New_York")
    current = now or datetime.now(UTC)
    expected = current.astimezone(eastern) + timedelta(seconds=30)
    if expected.second or expected.microsecond:
        expected = expected.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return expected.strftime("%-I:%M %p ET")
