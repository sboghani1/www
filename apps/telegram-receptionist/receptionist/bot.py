from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .database import Database
from .runner import AgentRunner

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("receptionist")

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


class Receptionist:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self.runner: AgentRunner | None = None

    async def post_init(self, application: Application) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.database.initialize(self.config.repositories)
        recovered = self.database.recover_interrupted_runs()
        self.database.prune_events()
        self.runner = AgentRunner(self.config, self.database, application.bot)
        self.runner.start()
        application.job_queue.run_repeating(
            self._heartbeat, interval=15, first=0
        )
        application.job_queue.run_repeating(
            self._deployment_poll, interval=15, first=5, data=application
        )
        await self._notify_deployment(application)
        log.info("Receptionist started; recovered_runs=%d", recovered)

    async def post_shutdown(self, application: Application) -> None:
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
            f"Repository: {state['repository_name']}\n"
            f"Session: {state['session_name'] or 'none'}\n"
            f"Provider: {state['session_provider'] or 'claude'}\n\n"
            "Send plain text to run an exact agent turn. Use /help for commands."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "/repos — list repositories\n"
            "/repo <name> — select repository\n"
            "/new [name] — start a new conversation\n"
            "/sessions — list conversations\n"
            "/switch <id-prefix> — switch conversation\n"
            "/reset — archive this conversation and start fresh\n"
            "/status — show queue and last run\n"
            "/stop — stop the active run\n"
            "/verbose [on|off] — toggle detailed resource updates\n"
            "/provider — show provider\n\n"
            "Any other text is passed unchanged as the next agent prompt."
        )

    async def repos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        state = self.database.get_user_state(update.effective_user.id)
        lines = ["Repositories:"]
        for repository in self.database.list_repositories():
            marker = "●" if repository["id"] == state["active_repository_id"] else "○"
            availability = "ready" if Path(repository["absolute_path"]).is_dir() else "missing"
            lines.append(f"{marker} {repository['name']} ({availability})")
        await update.message.reply_text("\n".join(lines))

    async def repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self.repos(update, context)
            return
        try:
            repository = self.database.select_repository(
                update.effective_user.id, context.args[0]
            )
        except LookupError as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"Selected {repository['name']}. "
            "Use /new to start a fresh conversation."
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
            f"Repository: {state['repository_name']}",
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

    async def _deployment_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._notify_deployment(context.job.data)

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
    ]
    for name, handler in commands:
        application.add_handler(
            CommandHandler(name, receptionist.authorized(handler))
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
