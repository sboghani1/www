from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .database import Database
from .notifier import systemd_notify
from .runner import AgentRunner

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("receptionist")

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


class Receptionist:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self.runner: AgentRunner | None = None
        self._deployment_lock = asyncio.Lock()
        self._deployment_tasks: set[asyncio.Task[None]] = set()
        self._deployment_process: asyncio.subprocess.Process | None = None

    async def post_init(self, application: Application) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.database.initialize(self.config.repositories)
        recovered = self.database.recover_interrupted_runs()
        recovered_deployments = self.database.recover_interrupted_deployments()
        self.database.prune_events()
        self.runner = AgentRunner(self.config, self.database, application.bot)
        self.runner.start()
        application.job_queue.run_repeating(
            self._heartbeat, interval=15, first=0
        )
        application.job_queue.run_repeating(
            self._deployment_poll, interval=15, first=5, data=application
        )
        application.job_queue.run_repeating(
            self._approval_request_poll, interval=5, first=1, data=application
        )
        await self._notify_deployment(application)
        systemd_notify("READY=1\nSTATUS=Telegram polling and agent worker ready")
        log.info(
            "Receptionist started; recovered_runs=%d recovered_deployments=%d",
            recovered,
            recovered_deployments,
        )

    async def post_shutdown(self, application: Application) -> None:
        if self._deployment_process and self._deployment_process.returncode is None:
            self._deployment_process.terminate()
            try:
                await asyncio.wait_for(self._deployment_process.wait(), timeout=10)
            except TimeoutError:
                pass
        if self.runner:
            await self.runner.close()

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
                    "Rejected Telegram update user=%s chat_type=%s",
                    user.id if user else None,
                    chat.type if chat else None,
                )
                return
            self.database.ensure_user_state(user.id, chat.id)
            await handler(update, context)

        return wrapped

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        state = self.database.get_user_state(update.effective_user.id)
        await update.message.reply_text(
            "🤖 Telegram Receptionist\n"
            "Workspace: /home/receptionist/repos\n"
            f"Session: {state['session_name'] or 'none'}\n"
            f"Provider: {state['session_provider'] or 'claude'}\n\n"
            "Send plain text to run an exact agent turn. Use /help for commands."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "/repos — list repositories currently in the workspace\n"
            "/new [name] — start a new conversation\n"
            "/sessions — list conversations\n"
            "/switch <id-prefix> — switch conversation\n"
            "/reset — archive this conversation and start fresh\n"
            "/status — show queue and last run\n"
            "/stop — stop the active run\n"
            "/verbose [on|off] — toggle detailed resource updates\n"
            "/provider — show provider\n\n"
            "/approve — execute the only pending deployment request\n"
            "/deny — reject the only pending deployment request\n"
            "/deployments — show recent deployment requests\n\n"
            "Deployment messages also include Approve and Deny buttons.\n\n"
            "Any other text is passed unchanged as the next agent prompt."
        )

    async def repos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        repositories = sorted(
            child.name
            for child in self.config.repo_root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )
        lines = ["Workspace repositories:"]
        lines.extend(f"• {name}" for name in repositories)
        if not repositories:
            lines.append("(none yet)")
        await update.message.reply_text("\n".join(lines))

    async def repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Repository switching is no longer required. Every session runs at "
            "/home/receptionist/repos and can work across all child repositories."
        )

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        name = " ".join(context.args).strip() or None
        session = self.database.create_session(update.effective_user.id, name)
        await update.message.reply_text(
            f"New session {session['id'][:8]} in {session['repository_name']}: "
            f"{session['display_name']}"
        )

    async def sessions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        state = self.database.get_user_state(update.effective_user.id)
        sessions = self.database.list_sessions(
            update.effective_user.id, state["active_repository_id"]
        )
        if not sessions:
            await update.message.reply_text("No sessions in this repository.")
            return
        lines = [f"Sessions for {state['repository_name']}:"]
        for session in sessions[:10]:
            marker = "●" if session["id"] == state["active_session_id"] else "○"
            lines.append(
                f"{marker} {session['id'][:8]} · {session['display_name']} "
                f"· {session['status']}"
            )
        await update.message.reply_text("\n".join(lines))

    async def switch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /switch <session-id-prefix>")
            return
        try:
            session = self.database.switch_session(
                update.effective_user.id, context.args[0]
            )
        except LookupError as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"Switched to {session['id'][:8]} · {session['display_name']}"
        )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.database.reset_session(update.effective_user.id)
        await update.message.reply_text(
            f"Context reset. New session: {session['id'][:8]}"
        )

    async def provider(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(
            "Provider: Claude Code CLI\n"
            "Copilot and Codex adapters are planned but not enabled."
        )

    async def verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        state = self.database.get_user_state(update.effective_user.id)
        if context.args and context.args[0].lower() in {"on", "off"}:
            enabled = context.args[0].lower() == "on"
        else:
            enabled = not bool(state["verbose"])
        self.database.set_verbose(update.effective_user.id, enabled)
        await update.message.reply_text(
            f"Verbose progress {'enabled' if enabled else 'disabled'}."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        state = self.database.get_user_state(update.effective_user.id)
        active = self.database.active_run()
        last = self.database.last_run_for_user(update.effective_user.id)
        lines = [
            "Workspace: /home/receptionist/repos",
            f"Session: {state['session_name'] or 'none'}",
            f"Queued: {self.database.queued_count()}",
            f"Verbose: {'on' if state['verbose'] else 'off'}",
        ]
        if active:
            lines.append(f"Active run: {active['id'][:8]}")
        if last:
            lines.append(f"Last run: {last['id'][:8]} · {last['status']}")
        await update.message.reply_text("\n".join(lines))

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        stopped = bool(self.runner and await self.runner.cancel_active())
        await update.message.reply_text(
            "Stopping the active run." if stopped else "No active run."
        )

    async def approve(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        _, message = await self._approve_deployment(
            update.effective_user.id,
            update.effective_chat.id,
            context.args[0] if context.args else "",
            context.application,
        )
        await update.message.reply_text(message)

    async def deny(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _, message = self._deny_deployment(
            update.effective_user.id,
            context.args[0] if context.args else "",
        )
        await update.message.reply_text(message)

    async def deployment_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        try:
            _, action, request_id = query.data.split(":", 2)
        except (AttributeError, ValueError):
            await query.message.reply_text("Invalid deployment action.")
            return
        if action == "approve":
            succeeded, message = await self._approve_deployment(
                update.effective_user.id,
                update.effective_chat.id,
                request_id,
                context.application,
            )
        elif action == "deny":
            succeeded, message = self._deny_deployment(
                update.effective_user.id, request_id
            )
        else:
            succeeded, message = False, "Invalid deployment action."
        if succeeded:
            await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(message)

    async def _approve_deployment(
        self,
        user_id: int,
        chat_id: int,
        request_prefix: str,
        application: Application,
    ) -> tuple[bool, str]:
        self.database.expire_deployment_requests()
        try:
            request = self.database.find_deployment_request(
                user_id, request_prefix, ("pending",)
            )
        except LookupError:
            return (
                False,
                "Approval requires exactly one pending request. "
                "Use the Approve button on the request you want.",
            )
        if not self.database.approve_deployment_request(request["id"]):
            return False, "That request expired or is no longer pending."
        request = self.database.find_deployment_request(
            user_id, request["id"], ("approved",)
        )
        task = asyncio.create_task(
            self._execute_approved_deployment(request, chat_id, application)
        )
        self._deployment_tasks.add(task)
        task.add_done_callback(self._deployment_tasks.discard)
        return (
            True,
            f"Approved deployment {request['id'][:8]}; execution is starting.",
        )

    def _deny_deployment(
        self, user_id: int, request_prefix: str
    ) -> tuple[bool, str]:
        self.database.expire_deployment_requests()
        try:
            request = self.database.find_deployment_request(
                user_id, request_prefix, ("pending",)
            )
        except LookupError:
            return (
                False,
                "Denial requires exactly one pending request. "
                "Use the Deny button on the request you want.",
            )
        if self.database.deny_deployment_request(request["id"]):
            return (
                True,
                f"Denied deployment {request['id'][:8]}. It cannot be reused.",
            )
        return False, "That request is no longer pending."

    async def deployments(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        requests = self.database.recent_deployment_requests(
            update.effective_user.id
        )
        if not requests:
            await update.message.reply_text("No deployment requests.")
            return
        lines = ["Recent deployment requests:"]
        for request in requests:
            lines.append(
                f"{request['id'][:8]} · {request['status']} · "
                f"{Path(request['repository_path']).name} · {request['summary'][:80]}"
            )
        await update.message.reply_text("\n".join(lines))

    async def exact_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        if message is None or message.text is None:
            return
        if self.database.queued_count() >= self.config.max_queued_messages:
            await message.reply_text(
                f"Queue is full ({self.config.max_queued_messages} messages)."
            )
            return
        session = self.database.get_or_create_active_session(
            update.effective_user.id
        )
        run = self.database.enqueue_run(
            session["id"], update.effective_chat.id, message.text
        )
        position = self.database.queued_count()
        await message.reply_text(
            f"👀 Received exactly as run {run['id'][:8]} "
            f"({session['repository_name']}, queue position {position})."
        )
        assert self.runner is not None
        self.runner.notify()

    async def unsupported(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text("V1 accepts text messages only.")

    async def _heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.runner is None or not self.runner.healthy:
            log.error("Agent worker is not alive; withholding heartbeat")
            return
        self.config.heartbeat_path.write_text(
            datetime.now(UTC).isoformat(), encoding="utf-8"
        )
        systemd_notify("WATCHDOG=1\nSTATUS=Telegram and agent worker healthy")

    async def _deployment_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._notify_deployment(context.job.data)

    async def _approval_request_poll(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._ingest_deployment_requests()
        self.database.expire_deployment_requests()
        state = self.database.ensure_user_state(
            self.config.allowed_user_id, self.config.allowed_user_id
        )
        chat_id = state["telegram_chat_id"]
        if not chat_id:
            return
        for request in self.database.unnotified_deployment_requests():
            text = (
                f"🚦 Deployment approval requested: {request['id'][:8]}\n\n"
                f"Summary: {request['summary']}\n"
                f"Repository: {request['repository_path']}\n"
                f"Revision: {request['revision']}\n"
                f"Expires: {request['expires_at']}\n\n"
                f"Exact root command:\n{request['command']}\n\n"
                "Use the buttons below, or send /approve or /deny when this is "
                "the only pending request."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Approve",
                            callback_data=f"deploy:approve:{request['id']}",
                        ),
                        InlineKeyboardButton(
                            "Deny",
                            callback_data=f"deploy:deny:{request['id']}",
                        ),
                    ]
                ]
            )
            await context.application.bot.send_message(
                chat_id, text, reply_markup=keyboard
            )
            self.database.mark_deployment_request_notified(request["id"])

    async def _ingest_deployment_requests(self) -> None:
        request_dir = self.config.deploy_request_dir
        if not request_dir.is_dir():
            return
        for path in sorted(request_dir.glob("*.json"))[:20]:
            try:
                if path.is_symlink() or path.stat().st_size > 8192:
                    raise ValueError("request file is unsafe")
                payload = json.loads(path.read_text(encoding="utf-8"))
                request_id = str(uuid.UUID(str(payload["id"])))
                if path.stem != request_id:
                    raise ValueError("request ID does not match filename")
                repository = Path(payload["repository_path"]).resolve()
                if not repository.is_relative_to(self.config.repo_root):
                    raise ValueError("repository is outside workspace")
                revision = str(payload["revision"])
                if not re.fullmatch(r"[0-9a-f]{40}", revision):
                    raise ValueError("invalid revision")
                command = str(payload["command"])
                summary = str(payload["summary"])
                if not command or len(command) > 2000 or "\0" in command:
                    raise ValueError("invalid command")
                if not summary or len(summary) > 500:
                    raise ValueError("invalid summary")
                created_at = datetime.fromisoformat(str(payload["created_at"]))
                expires_at = datetime.fromisoformat(str(payload["expires_at"]))
                if (
                    created_at.tzinfo is None
                    or expires_at.tzinfo is None
                    or expires_at <= created_at
                    or expires_at - created_at > timedelta(minutes=15)
                ):
                    raise ValueError("invalid request lifetime")
                created_at = created_at.astimezone(UTC)
                expires_at = expires_at.astimezone(UTC)
                self.database.import_deployment_request(
                    request_id=request_id,
                    user_id=self.config.allowed_user_id,
                    repository_path=str(repository),
                    revision=revision,
                    command=command,
                    summary=summary,
                    created_at=created_at.isoformat(),
                    expires_at=expires_at.isoformat(),
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                log.exception("Rejected deployment request file %s", path)
            finally:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    async def _execute_approved_deployment(
        self,
        request: dict,
        chat_id: int,
        application: Application,
    ) -> None:
        async with self._deployment_lock:
            if not self.database.start_deployment_request(request["id"]):
                await application.bot.send_message(
                    chat_id, "Deployment request was already consumed."
                )
                return
            payload = dict(request)
            payload["status"] = "approved"
            try:
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/sudo",
                    "-n",
                    self.config.deploy_executor,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as error:
                self.database.finish_deployment_request(
                    request["id"],
                    status="failed",
                    exit_code=None,
                    output=None,
                    error=f"Could not start deployment executor: {error}",
                )
                await application.bot.send_message(
                    chat_id,
                    f"❌ Deployment {request['id'][:8]} failed to start.",
                )
                return
            self._deployment_process = process
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    ),
                    timeout=self.config.deploy_timeout_seconds,
                )
                output = (
                    stdout.decode("utf-8", errors="replace")
                    + stderr.decode("utf-8", errors="replace")
                ).strip()
                output = output.replace(self.config.telegram_token, "[redacted]")
                if process.returncode == 0:
                    status = "succeeded"
                    error = None
                else:
                    status = "failed"
                    error = f"Executor exited with status {process.returncode}."
            except TimeoutError:
                process.terminate()
                await process.wait()
                output = ""
                status = "failed"
                error = (
                    f"Deployment exceeded {self.config.deploy_timeout_seconds} seconds."
                )
            finally:
                self._deployment_process = None
            self.database.finish_deployment_request(
                request["id"],
                status=status,
                exit_code=process.returncode,
                output=output[-8000:] if output else None,
                error=error,
            )
            message = (
                f"{'✅' if status == 'succeeded' else '❌'} Deployment "
                f"{request['id'][:8]} {status}."
            )
            if error:
                message += f"\n{error}"
            if output:
                message += f"\n\nLast output:\n{output[-2500:]}"
            await application.bot.send_message(chat_id, message)

    async def _notify_deployment(self, application: Application) -> None:
        path = self.config.state_dir / "deployment.json"
        if not path.exists():
            return
        try:
            deployment = json.loads(path.read_text(encoding="utf-8"))
            deployment_id = str(deployment["id"])
        except (OSError, ValueError, KeyError):
            return
        if self.database.deployment_is_seen(deployment_id):
            return
        state = self.database.ensure_user_state(
            self.config.allowed_user_id, self.config.allowed_user_id
        )
        chat_id = state["telegram_chat_id"]
        if not chat_id:
            return
        await application.bot.send_message(
            chat_id,
            f"🚀 Receptionist deployment {deployment.get('status', 'unknown')}\n"
            f"Revision: {str(deployment.get('revision', 'unknown'))[:12]}\n"
            f"{deployment.get('message', '')}".strip(),
        )
        self.database.mark_deployment_seen(deployment_id)


def build_application(config: Config) -> Application:
    database = Database(config.database_path)
    receptionist = Receptionist(config, database)
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(receptionist.post_init)
        .post_shutdown(receptionist.post_shutdown)
        .build()
    )
    commands: list[tuple[str, Handler]] = [
        ("start", receptionist.start),
        ("help", receptionist.help),
        ("repos", receptionist.repos),
        ("repo", receptionist.repo),
        ("new", receptionist.new),
        ("sessions", receptionist.sessions),
        ("switch", receptionist.switch),
        ("reset", receptionist.reset),
        ("provider", receptionist.provider),
        ("verbose", receptionist.verbose),
        ("status", receptionist.status),
        ("stop", receptionist.stop),
        ("approve", receptionist.approve),
        ("deny", receptionist.deny),
        ("deployments", receptionist.deployments),
    ]
    for name, handler in commands:
        application.add_handler(
            CommandHandler(name, receptionist.authorized(handler))
        )
    application.add_handler(
        CallbackQueryHandler(
            receptionist.authorized(receptionist.deployment_button),
            pattern=r"^deploy:(approve|deny):[0-9a-f-]{36}$",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receptionist.authorized(receptionist.exact_text),
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.TEXT, receptionist.authorized(receptionist.unsupported)
        )
    )
    return application


def main() -> None:
    config = Config.from_env()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    build_application(config).run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
