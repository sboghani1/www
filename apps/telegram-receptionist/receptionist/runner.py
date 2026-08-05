from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

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
        return self._worker is not None and not self._worker.done()

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

    async def _work_loop(self) -> None:
        while True:
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
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.config.agent_timeout_seconds
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
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)

        if result.session_id:
            self.database.update_provider_session(
                run["session_id"], result.session_id
            )

        stderr = "\n".join(stderr_lines[-30:]).strip()
        if self._cancel_requested:
            status = "cancelled"
            error = "Cancelled from Telegram."
        elif timed_out:
            status = "timed_out"
            error = (
                f"Run exceeded {self.config.agent_timeout_seconds // 60} minutes."
            )
        elif process.returncode == 0 and result.final_response and not result.is_error:
            status = "succeeded"
            error = None
        else:
            status = "failed"
            error = stderr or "Claude exited without a final response."

        self.database.finish_run(
            run["id"],
            status=status,
            exit_code=process.returncode,
            final_response=result.final_response or None,
            error=error,
            usage=result.usage,
        )
        await self._finish_telegram(
            run, status_message.message_id, status, result.final_response, error
        )
        self._active_process = None
        self._active_run_id = None
        self._cancel_requested = False

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
                f"{result.activity}\nElapsed: {elapsed // 60}m {elapsed % 60}s"
            )
            if verbose:
                text += "\n" + resource_summary(process.pid)
            await self._safe_edit(run["telegram_chat_id"], message_id, text)

    async def _finish_telegram(
        self,
        run: dict[str, Any],
        message_id: int,
        status: str,
        response: str,
        error: str | None,
    ) -> None:
        icons = {
            "succeeded": "✅",
            "failed": "❌",
            "cancelled": "🛑",
            "timed_out": "⌛",
        }
        await self._safe_edit(
            run["telegram_chat_id"],
            message_id,
            f"{icons.get(status, '•')} Run {run['id'][:8]} {status}.",
        )
        if status != "succeeded":
            await self.bot.send_message(
                run["telegram_chat_id"], error or "The run did not complete."
            )
            return
        if len(response) > ATTACHMENT_THRESHOLD:
            await self.bot.send_document(
                run["telegram_chat_id"],
                text_attachment(response, f"run-{run['id'][:8]}.txt"),
                caption="The complete agent response is attached.",
            )
            return
        for chunk in split_message(response):
            await self.bot.send_message(run["telegram_chat_id"], chunk)

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
