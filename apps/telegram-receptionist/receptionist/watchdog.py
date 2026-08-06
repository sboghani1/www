from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

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
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("receptionist-watchdog")

WATCHDOG_HELPER = "/usr/local/libexec/receptionist-watchdog-helper"
WATCHDOG_CALLBACK_PATTERN = (
    r"^watchdog:(?:ping|status|restart|restart-confirm|logs|mem|menu)$"
)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


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

    async def post_init(self, application: Application) -> None:
        await application.bot.set_my_commands(
            [
                BotCommand("ping", "Check watchdog liveness"),
                BotCommand("status", "Show receptionist health"),
                BotCommand("restart", "Restart the receptionist"),
                BotCommand("logs", "Show recent receptionist logs"),
                BotCommand("mem", "Show VPS memory and swap"),
                BotCommand("help", "Show watchdog commands"),
            ]
        )

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
        text = stdout[-3800:] or "(no recent logs)"
        await update.effective_message.reply_text(
            f"Recent receptionist logs:\n{text}",
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
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        return f"Receptionist status was not valid JSON:\n{raw[-1000:]}"
    active = status.get("active_state", "unknown")
    sub = status.get("sub_state", "unknown")
    revision = str(status.get("revision") or "unknown")[:12]
    drain = "yes" if status.get("deployment_drain") else "no"
    return (
        "Receptionist status\n"
        f"Service: {active} ({sub})\n"
        f"PID: {status.get('main_pid', 'unknown')}\n"
        f"Restarts: {status.get('restarts', 'unknown')}\n"
        f"Revision: {revision}\n"
        f"Deployment drain: {drain}"
    )


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
                )
            ],
        ]
    )


def build_application(config: WatchdogConfig) -> Application:
    watchdog = ReceptionistWatchdog(config)
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(watchdog.post_init)
        .build()
    )
    for command, handler in (
        ("ping", watchdog.ping),
        ("status", watchdog.status),
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
    return application


def main() -> None:
    build_application(WatchdogConfig.from_env()).run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
