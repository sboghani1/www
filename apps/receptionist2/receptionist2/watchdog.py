from __future__ import annotations

import asyncio
import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import EFFORT_LEVELS, WatchdogConfig, resolve_model
from .database import Database

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("receptionist2.watchdog")
logging.getLogger("httpx").setLevel(logging.WARNING)

REAUTH_TIMEOUT_SECONDS = 300
CLAUDE_AUTH_URL_PREFIX = "https://claude.com/"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HELP_TEXT = (
    "Receptionist2 watchdog\n"
    "/status — service, database, and provider health\n"
    "/logs — recent gateway and worker logs\n"
    "/restart — drain and restart the gateway and worker\n"
    "/diagnose — run the root diagnostic agent\n"
    "/investigate <problem> — investigate a specific problem\n"
    "/reauth — reauthenticate the Claude provider\n"
    "/model [name] — show or set the active session model\n"
    "/effort <level> — set the active session effort"
)


class Watchdog:
    def __init__(self, config: WatchdogConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self._restart_lock = asyncio.Lock()
        self._diagnostic_tasks: set[asyncio.Task] = set()
        self._reauth_process: asyncio.subprocess.Process | None = None
        self._reauth_timeout_task: asyncio.Task | None = None
        self._application: Application | None = None

    async def post_init(self, application: Application) -> None:
        self._application = application
        self.database.initialize()
        application.job_queue.run_repeating(
            self.restart_poll, interval=5, first=1
        )

    def authorized(self, handler):
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
                log.warning("Rejected watchdog update")
                return
            await handler(update, context)

        return wrapped

    async def _helper(
        self,
        action: str,
        argument: str | None = None,
        timeout: int | None = 120,
    ) -> tuple[int, str, str]:
        argv = ["/usr/bin/sudo", "-n", self.config.helper, action]
        if argument:
            argv.append(argument)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "", "helper timed out"
        return (
            process.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    async def status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        code, stdout, stderr = await self._helper("status")
        if code:
            await update.effective_message.reply_text(
                f"Status failed: {stderr[-1000:]}"
            )
            return
        payload = json.loads(stdout)
        await update.effective_message.reply_text(
            "Receptionist2\n"
            f"Gateway: {payload['gateway']['active']} "
            f"({payload['gateway']['sub']})\n"
            f"Worker: {payload['worker']['active']} "
            f"({payload['worker']['sub']})\n"
            f"Database: {payload['database']['quick_check']}\n"
            f"Provider: {payload['provider']['condition']}\n"
            f"Gateway ready: {payload['readiness']['gateway']}\n"
            f"Worker ready: {payload['readiness']['worker']}\n"
            f"Queued: {payload['queue'].get('queued', 0)}\n"
            f"Running: {payload['queue'].get('running', 0)}\n"
            f"Pending deliveries: "
            f"{payload['queue'].get('outbox_pending', 0)}"
        )

    async def logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        code, stdout, stderr = await self._helper("logs")
        if code:
            await update.effective_message.reply_text(
                f"Logs failed: {stderr[-1000:]}"
            )
            return
        payload = json.loads(stdout)
        text = "\n".join(payload.get("lines", [])) or "(no recent logs)"
        await update.effective_message.reply_text(text[-3900:])

    async def restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "How long should the worker drain?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "10 minutes", callback_data="r2wd:restart:600"
                        ),
                        InlineKeyboardButton(
                            "No timeout", callback_data="r2wd:restart:wait"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Interrupt now", callback_data="r2wd:restart:0"
                        )
                    ],
                ]
            ),
        )

    async def restart_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        value = str(query.data).rsplit(":", 1)[-1]
        timeout = None if value == "wait" else int(value)
        request = self.database.create_restart_request(
            update.effective_user.id,
            update.effective_chat.id,
            timeout,
        )
        await query.edit_message_text(
            f"⚡ Restart {request['id'][:8]} queued."
        )

    async def help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(HELP_TEXT)

    async def diagnose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._start_incident(update, context, None)

    async def investigate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        problem = " ".join(context.args or []).strip()
        if not problem:
            await update.effective_message.reply_text(
                "Usage: /investigate <problem description>"
            )
            return
        await self._start_incident(update, context, problem)

    async def _start_incident(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        problem: str | None,
    ) -> None:
        incident = self.database.create_incident(
            update.effective_chat.id, problem
        )
        kind = "Investigation" if problem else "Diagnostic incident"
        await update.effective_message.reply_text(
            f"🔎 {kind} {incident['id'][:8]} started. "
            "The agent has full operator and root authority."
        )
        task = asyncio.create_task(
            self._run_diagnosis(
                incident["id"],
                update.effective_chat.id,
                context.application,
            )
        )
        self._diagnostic_tasks.add(task)
        task.add_done_callback(self._diagnostic_tasks.discard)

    async def _run_diagnosis(
        self, incident_id: str, chat_id: int, application: Application
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo",
            "-n",
            self.config.diagnostic_runner,
            incident_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=1900
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            self.database.finish_incident(
                incident_id, error="Diagnostic timed out"
            )
            await application.bot.send_message(
                chat_id,
                "❌ Diagnostic agent timed out."
            )
            return
        if process.returncode:
            await application.bot.send_message(
                chat_id,
                "❌ Diagnostic failed:\n"
                + stderr.decode(errors="replace")[-3000:]
            )
            return
        report = stdout.decode(errors="replace").strip()
        for start in range(0, len(report), 3900):
            await application.bot.send_message(
                chat_id,
                report[start : start + 3900]
            )

    async def reauth(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.effective_message.reply_text(
            "Start a private Claude authentication session?\n\n"
            "The next plain-text message you send to this watchdog will be "
            "relayed as the one-time authorization code and deleted when "
            "possible.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Start reauthentication",
                            callback_data="r2wd:reauth:confirm",
                        ),
                        InlineKeyboardButton(
                            "Cancel", callback_data="r2wd:reauth:cancel"
                        ),
                    ]
                ]
            ),
        )

    async def reauth_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        if str(query.data).endswith(":cancel"):
            await query.edit_message_text("Reauthentication cancelled.")
            return
        await query.edit_message_text(
            "Starting a private Claude authentication session…"
        )
        message = update.effective_message
        if self._reauth_process is not None:
            await message.reply_text(
                "A Claude authentication session is already waiting for a "
                "response."
            )
            return
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
            await message.reply_text(
                f"❌ Could not start Claude authentication: {error}"
            )
            return
        self._reauth_process = process
        try:
            output = await read_reauth_prompt(process)
            auth_url = extract_auth_url(output)
        except (TimeoutError, RuntimeError) as error:
            await self._stop_reauth()
            await message.reply_text(
                f"❌ Could not start Claude authentication: {error}"
            )
            return
        self._reauth_timeout_task = asyncio.create_task(
            self._expire_reauth(update.effective_chat.id)
        )
        await message.reply_text(
            "Open this link and authorize Claude:\n\n"
            f"{auth_url}\n\n"
            "Send the one-time code as your next message here. It is relayed "
            "directly to Claude and deleted from Telegram when possible.",
            disable_web_page_preview=True,
        )

    async def reauth_response(
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
            process.stdin.close()
            await finish_reauth_process(process)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            await self._stop_reauth()
            await message.reply_text(
                "❌ Claude authentication did not accept the response. "
                "Start a new /reauth attempt."
            )
            return
        finally:
            response = ""
        self._reauth_process = None
        code, stdout, _ = await self._helper("auth-status", timeout=60)
        try:
            logged_in = json.loads(stdout).get("logged_in") if not code else None
        except (json.JSONDecodeError, AttributeError):
            logged_in = None
        if logged_in is True:
            log.info("Claude reauthentication succeeded")
            await message.reply_text(
                "✅ Claude reauthentication succeeded. No restart is required."
            )
            return
        log.warning("Claude reauthentication failed")
        await message.reply_text(
            "❌ Claude is still not authenticated. Start a new /reauth "
            "attempt."
        )

    async def _expire_reauth(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(REAUTH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        await self._stop_reauth()
        if self._application is not None:
            await self._application.bot.send_message(
                chat_id, "Claude reauthentication expired after five minutes."
            )

    async def _stop_reauth(self) -> None:
        timeout_task = self._reauth_timeout_task
        self._reauth_timeout_task = None
        if timeout_task is not None and timeout_task is not asyncio.current_task():
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

    async def model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            session = self.database.active_session(update.effective_user.id)
            await update.effective_message.reply_text(
                f"Active session model: {session['model']}"
            )
            return
        try:
            model = resolve_model(context.args[0])
            session = self.database.set_session_runtime(
                update.effective_user.id, model=model
            )
        except (ValueError, LookupError) as error:
            await update.effective_message.reply_text(str(error))
            return
        await update.effective_message.reply_text(
            f"Model set to {session['model']} for future runs."
        )

    async def effort(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        level = context.args[0].lower() if context.args else ""
        if level not in EFFORT_LEVELS:
            await update.effective_message.reply_text(
                f"Usage: /effort {'|'.join(EFFORT_LEVELS)}"
            )
            return
        try:
            self.database.set_session_runtime(
                update.effective_user.id, effort=level
            )
        except LookupError as error:
            await update.effective_message.reply_text(str(error))
            return
        await update.effective_message.reply_text(
            f"Effort set to {level} for future runs."
        )

    async def restart_poll(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._restart_lock.locked():
            return
        request = self.database.claim_restart_request()
        if request is None:
            return
        async with self._restart_lock:
            code, _, stderr = await self._helper(
                "restart",
                request["id"],
                timeout=(
                    None
                    if request["timeout_seconds"] is None
                    else int(request["timeout_seconds"]) + 180
                ),
            )
            if code:
                log.error(
                    "Restart %s failed: %s", request["id"], stderr[-1000:]
                )


async def read_reauth_prompt(process: asyncio.subprocess.Process) -> str:
    if process.stdout is None:
        raise RuntimeError("authentication output was unavailable")
    loop = asyncio.get_running_loop()
    chunks: list[bytes] = []
    deadline = loop.time() + 30
    while loop.time() < deadline:
        chunk = await asyncio.wait_for(
            process.stdout.read(4096), deadline - loop.time()
        )
        if not chunk:
            break
        chunks.append(chunk)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if "Paste code here" in output:
            return output
    raise RuntimeError("Claude did not provide an authorization URL")


def extract_auth_url(output: str) -> str:
    cleaned = ANSI_ESCAPE.sub("", output)
    start = cleaned.find("https://")
    end = cleaned.find("Paste code here", start)
    if start < 0 or end < 0:
        raise RuntimeError("Claude did not provide an authorization URL")
    auth_url = "".join(cleaned[start:end].split()).rstrip(">")
    if not auth_url.startswith(CLAUDE_AUTH_URL_PREFIX):
        raise RuntimeError("Claude returned an unexpected authorization URL")
    return auth_url


async def finish_reauth_process(process: asyncio.subprocess.Process) -> None:
    readers = [
        asyncio.create_task(stream.read())
        for stream in (process.stdout, process.stderr)
        if stream is not None
    ]
    try:
        await asyncio.wait_for(process.wait(), timeout=90)
    except TimeoutError:
        process.terminate()
        await process.wait()
        raise
    finally:
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)


def build_application(config: WatchdogConfig) -> Application:
    watchdog = Watchdog(config, Database(config.database_path))
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(watchdog.post_init)
        .build()
    )
    for name, handler in (
        ("status", watchdog.status),
        ("logs", watchdog.logs),
        ("restart", watchdog.restart),
        ("diagnose", watchdog.diagnose),
        ("investigate", watchdog.investigate),
        ("reauth", watchdog.reauth),
        ("model", watchdog.model),
        ("effort", watchdog.effort),
        ("help", watchdog.help),
        ("start", watchdog.help),
    ):
        application.add_handler(
            CommandHandler(name, watchdog.authorized(handler))
        )
    application.add_handler(
        CallbackQueryHandler(
            watchdog.authorized(watchdog.restart_button),
            pattern=r"^r2wd:restart:(?:600|wait|0)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            watchdog.authorized(watchdog.reauth_button),
            pattern=r"^r2wd:reauth:(?:confirm|cancel)$",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            watchdog.authorized(watchdog.reauth_response),
        )
    )
    return application


def main() -> None:
    config = WatchdogConfig.from_env()
    build_application(config).run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
