from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import psutil
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("receptionist-watchdog")

WATCHDOG_HELPER = "/usr/local/libexec/receptionist-watchdog-helper"
WATCHDOG_CALLBACK_PATTERN = (
    r"^watchdog:(?:ping|status|restart|restart-confirm|logs|mem|menu|"
    r"reauth|reauth-confirm)$"
)
REAUTH_TIMEOUT_SECONDS = 300
CLAUDE_AUTH_URL_PREFIX = "https://claude.com/"
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
EASTERN = ZoneInfo("America/New_York")
LOG_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ "
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) [^:]+: "
)
ROUTINE_LOG_MARKERS = (
    "apscheduler.executors.default:",
    "apscheduler.scheduler:",
    "telegram.ext.Application: Application started",
)


@dataclass(frozen=True)
class WatchdogConfig:
    telegram_token: str
    allowed_user_id: int
    helper: str = WATCHDOG_HELPER

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        token = os.getenv("WATCHDOG_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("WATCHDOG_BOT_TOKEN is required")
        user_id = os.getenv("WATCHDOG_ALLOWED_USER_ID", "").strip()
        if not user_id:
            raise RuntimeError("WATCHDOG_ALLOWED_USER_ID is required")
        return cls(telegram_token=token, allowed_user_id=int(user_id))


class ReceptionistWatchdog:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config
        self._application: Application | None = None
        self._reauth_process: asyncio.subprocess.Process | None = None
        self._reauth_timeout_task: asyncio.Task[None] | None = None

    async def post_init(self, application: Application) -> None:
        self._application = application
        await application.bot.set_my_commands(
            [
                BotCommand("ping", "Check watchdog liveness"),
                BotCommand("status", "Show receptionist health"),
                BotCommand("reauth", "Reauthenticate Claude"),
                BotCommand("restart", "Restart the receptionist"),
                BotCommand("logs", "Show recent receptionist logs"),
                BotCommand("mem", "Show VPS memory and swap"),
                BotCommand("help", "Show watchdog commands"),
            ]
        )

    async def post_shutdown(self, application: Application) -> None:
        await self._stop_reauthentication()

    def authorized(self, handler: Handler) -> Handler:
        async def wrapped(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            user = update.effective_user
            chat = update.effective_chat
            if (
                user is None
                or chat is None
                or chat.type != ChatType.PRIVATE
                or user.id != self.config.allowed_user_id
            ):
                log.warning(
                    "Rejected watchdog update user=%s chat_type=%s",
                    user.id if user else None,
                    chat.type if chat else None,
                )
                return
            await handler(update, context)

        return wrapped

    async def ping(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "pong", reply_markup=watchdog_menu()
        )

    async def status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        return_code, stdout, stderr = await self._run_helper("status")
        if return_code != 0:
            await update.effective_message.reply_text(
                f"Could not read receptionist status.\n{stderr[-1000:]}",
                reply_markup=watchdog_menu(),
            )
            return
        await update.effective_message.reply_text(
            _format_status(stdout), reply_markup=watchdog_menu()
        )

    async def confirm_reauthentication(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "Start a private Claude authentication session?\n\n"
            "The next plain-text message you send to this watchdog will be "
            "treated as the one-time authorization response and deleted when "
            "possible.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Start reauthentication",
                            callback_data="watchdog:reauth-confirm",
                        ),
                        InlineKeyboardButton(
                            "Cancel", callback_data="watchdog:menu"
                        ),
                    ]
                ]
            ),
        )

    async def start_reauthentication(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._reauth_process is not None:
            await update.effective_message.reply_text(
                "A Claude authentication session is already waiting for a "
                "response.",
                reply_markup=watchdog_menu(),
            )
            return

        await update.effective_message.reply_text(
            "Starting a private Claude authentication session…"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/sudo",
                "-n",
                self.config.helper,
                "reauth",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            await update.effective_message.reply_text(
                f"❌ Could not start Claude authentication: {error}",
                reply_markup=watchdog_menu(),
            )
            return
        self._reauth_process = process
        try:
            output = await _read_reauthentication_prompt(process)
            auth_url = _extract_auth_url(output)
        except (TimeoutError, RuntimeError) as error:
            await self._stop_reauthentication()
            await update.effective_message.reply_text(
                f"❌ Could not start Claude authentication: {error}",
                reply_markup=watchdog_menu(),
            )
            return

        self._reauth_timeout_task = asyncio.create_task(
            self._expire_reauthentication(update.effective_chat.id)
        )
        await update.effective_message.reply_text(
            "Open this link in your browser and authorize Claude:\n\n"
            f"{auth_url}\n\n"
            "If the browser displays a one-time code, send that code as your "
            "next message here. It will be relayed directly to Claude and "
            "deleted from Telegram when possible.",
            disable_web_page_preview=True,
        )

    async def reauthentication_response(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        process = self._reauth_process
        message = update.effective_message
        if process is None or process.stdin is None:
            return

        response = str(message.text or "").strip()
        if not response:
            return
        try:
            await message.delete()
        except TelegramError:
            pass

        timeout_task = self._reauth_timeout_task
        self._reauth_timeout_task = None
        if timeout_task is not None:
            timeout_task.cancel()

        try:
            process.stdin.write((response + "\n").encode("utf-8"))
            await process.stdin.drain()
            response = ""
            process.stdin.close()
            await _finish_reauthentication_process(process)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            await self._stop_reauthentication()
            await message.reply_text(
                "❌ Claude authentication did not accept the response. Start "
                "a new reauthentication attempt.",
                reply_markup=watchdog_menu(),
            )
            return
        finally:
            response = ""

        self._reauth_process = None
        return_code, stdout, _ = await self._run_helper("status")
        status = _parse_status(stdout) if return_code == 0 else {}
        if status.get("claude_logged_in") is True:
            log.info("Claude reauthentication succeeded")
            await message.reply_text(
                "✅ Claude reauthentication succeeded. The receptionist is "
                "ready; no restart is required.",
                reply_markup=watchdog_menu(),
            )
            return

        log.warning("Claude reauthentication failed")
        await message.reply_text(
            "❌ Claude is still not authenticated. Start a new "
            "reauthentication attempt.",
            reply_markup=watchdog_menu(),
        )

    async def _expire_reauthentication(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(REAUTH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        await self._stop_reauthentication()
        if self._application is None:
            return
        await self._application.bot.send_message(
            chat_id,
            "Claude reauthentication expired after five minutes.",
            reply_markup=watchdog_menu(),
        )

    async def _stop_reauthentication(self) -> None:
        timeout_task = self._reauth_timeout_task
        self._reauth_timeout_task = None
        current = asyncio.current_task()
        if timeout_task is not None and timeout_task is not current:
            timeout_task.cancel()
        process = self._reauth_process
        self._reauth_process = None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "Restarting the receptionist…"
        )
        return_code, stdout, stderr = await self._run_helper("restart", timeout=60)
        if return_code != 0:
            await update.effective_message.reply_text(
                f"❌ Restart failed.\n{stderr[-1000:]}",
                reply_markup=watchdog_menu(),
            )
            return
        await update.effective_message.reply_text(
            "✅ Restart completed.\n"
            f"{_format_status(stdout)}\n\n"
            "Continue in the main bot to resume the current session, send "
            "/recover for an interrupted run, or /reset for fresh context.",
            reply_markup=watchdog_menu(),
        )

    async def confirm_restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "Restart the main receptionist service?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Restart now",
                            callback_data="watchdog:restart-confirm",
                        ),
                        InlineKeyboardButton(
                            "Cancel", callback_data="watchdog:menu"
                        ),
                    ]
                ]
            ),
        )

    async def logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        return_code, stdout, stderr = await self._run_helper("logs")
        if return_code != 0:
            await update.effective_message.reply_text(
                f"Could not read receptionist logs.\n{stderr[-1000:]}",
                reply_markup=watchdog_menu(),
            )
            return
        await update.effective_message.reply_text(
            _format_logs(stdout),
            reply_markup=watchdog_menu(),
        )

    async def memory(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        await update.effective_message.reply_text(
            "VPS memory\n"
            f"RAM: {_mib(memory.used)} MiB used / {_mib(memory.total)} MiB "
            f"({_mib(memory.available)} MiB available)\n"
            f"Swap: {_mib(swap.used)} MiB used / {_mib(swap.total)} MiB",
            reply_markup=watchdog_menu(),
        )

    async def help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "/ping — check this independent bot\n"
            "/status — show receptionist health and deployed revision\n"
            "/reauth — reauthenticate the Claude provider\n"
            "/restart — restart only the receptionist service\n"
            "/logs — show recent receptionist journal lines\n"
            "/mem — show VPS RAM and swap\n\n"
            "Use the main receptionist's /recover for interrupted runs and "
            "/reset for a fresh Claude context.",
            reply_markup=watchdog_menu(),
        )

    async def button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        action = query.data.removeprefix("watchdog:")
        if action == "restart":
            await self.confirm_restart(update, context)
            return
        if action == "restart-confirm":
            await self.restart(update, context)
            return
        if action == "reauth":
            await self.confirm_reauthentication(update, context)
            return
        if action == "reauth-confirm":
            await self.start_reauthentication(update, context)
            return
        if action == "status":
            await self.status(update, context)
            return
        if action == "logs":
            await self.logs(update, context)
            return
        if action == "mem":
            await self.memory(update, context)
            return
        if action == "ping":
            await self.ping(update, context)
            return
        await query.message.reply_text(
            "Watchdog controls", reply_markup=watchdog_menu()
        )

    async def _run_helper(
        self, action: str, *, timeout: int = 15
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo",
            "-n",
            self.config.helper,
            action,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "", "Watchdog helper timed out."
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )


def _format_status(raw: str) -> str:
    status = _parse_status(raw)
    if not status:
        return f"Receptionist status was not valid JSON:\n{raw[-1000:]}"
    active = status.get("active_state", "unknown")
    sub = status.get("sub_state", "unknown")
    revision = str(status.get("revision") or "unknown")[:12]
    drain = "yes" if status.get("deployment_drain") else "no"
    claude_ready = (
        "ready"
        if status.get("claude_logged_in") is True
        else "authentication required"
        if status.get("claude_logged_in") is False
        else "unknown"
    )
    return (
        "Receptionist status\n"
        f"Service: {active} ({sub})\n"
        f"Claude provider: {claude_ready}\n"
        f"PID: {status.get('main_pid', 'unknown')}\n"
        f"Restarts: {status.get('restarts', 'unknown')}\n"
        f"Revision: {revision}\n"
        f"Deployment drain: {drain}"
    )


def _parse_status(raw: str) -> dict:
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return status if isinstance(status, dict) else {}


async def _read_reauthentication_prompt(
    process: asyncio.subprocess.Process,
) -> str:
    if process.stdout is None:
        raise RuntimeError("authentication output was unavailable")
    chunks: list[bytes] = []
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        chunk = await asyncio.wait_for(process.stdout.read(4096), remaining)
        if not chunk:
            break
        chunks.append(chunk)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if "Paste code here" in output:
            return output
    raise RuntimeError("Claude did not provide an authorization URL")


def _extract_auth_url(output: str) -> str:
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    start = cleaned.find("https://")
    end = cleaned.find("Paste code here", start)
    if start < 0 or end < 0:
        raise RuntimeError("Claude did not provide an authorization URL")
    auth_url = "".join(cleaned[start:end].split()).rstrip(">")
    if not auth_url.startswith(CLAUDE_AUTH_URL_PREFIX):
        raise RuntimeError("Claude returned an unexpected authorization URL")
    return auth_url


async def _finish_reauthentication_process(
    process: asyncio.subprocess.Process,
) -> None:
    readers = []
    if process.stdout is not None:
        readers.append(asyncio.create_task(process.stdout.read()))
    if process.stderr is not None:
        readers.append(asyncio.create_task(process.stderr.read()))
    try:
        await asyncio.wait_for(process.wait(), timeout=90)
    except TimeoutError:
        process.terminate()
        await process.wait()
        raise
    finally:
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)


def _format_logs(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return f"Could not parse receptionist logs:\n{raw[-1000:]}"
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return "Could not parse receptionist logs."

    lines = []
    hidden = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        priority = _priority(entry.get("priority"))
        if priority > 4 and any(
            marker in message for marker in ROUTINE_LOG_MARKERS
        ):
            hidden += 1
            continue
        timestamp = _log_time(entry.get("timestamp"))
        icon = "❌" if priority <= 3 else "⚠️" if priority == 4 else "•"
        cleaned = LOG_PREFIX.sub("", message.strip())
        cleaned = cleaned.replace("\n", "\n    ")
        if len(cleaned) > 700:
            cleaned = cleaned[:697] + "..."
        lines.append(f"{timestamp} {icon} {cleaned}")

    visible = lines[-15:]
    if not visible:
        body = "No warnings, errors, or user-visible activity in the last 2 hours."
    else:
        body = "\n\n".join(visible)
    suffix = (
        f"\n\n{hidden} routine scheduler entries hidden."
        if hidden
        else ""
    )
    return f"Recent receptionist activity\n\n{body}{suffix}"[:4000]


def _priority(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 6


def _log_time(value: object) -> str:
    try:
        timestamp = int(str(value)) / 1_000_000
    except (TypeError, ValueError):
        return "Unknown time"
    local = datetime.fromtimestamp(timestamp, UTC).astimezone(EASTERN)
    return local.strftime("%b %d %I:%M:%S %p").replace(" 0", " ")


def _mib(value: int) -> int:
    return round(value / 1024 / 1024)


def watchdog_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Status", callback_data="watchdog:status"
                ),
                InlineKeyboardButton(
                    "Memory", callback_data="watchdog:mem"
                ),
            ],
            [
                InlineKeyboardButton("Logs", callback_data="watchdog:logs"),
                InlineKeyboardButton("Ping", callback_data="watchdog:ping"),
            ],
            [
                InlineKeyboardButton(
                    "Restart receptionist",
                    callback_data="watchdog:restart",
                ),
                InlineKeyboardButton(
                    "Reauthenticate Claude",
                    callback_data="watchdog:reauth",
                ),
            ],
        ]
    )


def build_application(config: WatchdogConfig) -> Application:
    watchdog = ReceptionistWatchdog(config)
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(watchdog.post_init)
        .post_shutdown(watchdog.post_shutdown)
        .build()
    )
    for command, handler in (
        ("ping", watchdog.ping),
        ("status", watchdog.status),
        ("reauth", watchdog.confirm_reauthentication),
        ("restart", watchdog.confirm_restart),
        ("logs", watchdog.logs),
        ("mem", watchdog.memory),
        ("ram", watchdog.memory),
        ("help", watchdog.help),
        ("start", watchdog.help),
    ):
        application.add_handler(
            CommandHandler(command, watchdog.authorized(handler))
        )
    application.add_handler(
        CallbackQueryHandler(
            watchdog.authorized(watchdog.button),
            pattern=WATCHDOG_CALLBACK_PATTERN,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            watchdog.authorized(watchdog.reauthentication_response),
        )
    )
    return application


def main() -> None:
    build_application(WatchdogConfig.from_env()).run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
