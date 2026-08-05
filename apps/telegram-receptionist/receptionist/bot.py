from __future__ import annotations

import asyncio
import json
import logging
import os
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
from .runner import AgentRunner, process_group_alive
from .wnba import WNBA_TEMPLATE_HEADER, WnbaHelperClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("receptionist")
SELF_DEPLOY_WORKER = "/usr/local/libexec/deploy-telegram-receptionist-worker"

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
MAX_CALLBACK_BYTES = 64
WNBA_CALLBACK_PATTERN = re.compile(
    r"^wnba:(?:game:[A-Za-z0-9_.-]{1,48}|page:[0-9]{1,4}|"
    r"generate|copy|history|revisions|undo)$"
)


def wnba_callback(action: str, event_id: str = "") -> str:
    data = f"wnba:{action}" + (f":{event_id}" if event_id else "")
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("WNBA callback data exceeds Telegram limit")
    return data


def _wnba_matchup(game: dict) -> str:
    return f"{game.get('away_team', '')} @ {game.get('home_team', '')}"


WNBA_GAMES_PAGE_SIZE = 5


def wnba_page_games(
    games: list[dict], page: int
) -> tuple[list[dict], int, int]:
    page_count = max(
        1, (len(games) + WNBA_GAMES_PAGE_SIZE - 1) // WNBA_GAMES_PAGE_SIZE
    )
    normalized = min(max(page, 0), page_count - 1)
    start = normalized * WNBA_GAMES_PAGE_SIZE
    return (
        games[start : start + WNBA_GAMES_PAGE_SIZE],
        normalized,
        page_count,
    )


def wnba_games_header(games: list[dict], page: int) -> str:
    if not games:
        return "No upcoming WNBA games are available."
    _, normalized, page_count = wnba_page_games(games, page)
    return (
        "🏀 WNBA games in the next 14 days\n"
        f"Page {normalized + 1} of {page_count}"
    )


def wnba_games_markup(games: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    current, normalized, page_count = wnba_page_games(games, page)
    rows = []
    for game in current:
        label = (
            f"{game.get('commence_time_et', '')} · {_wnba_matchup(game)}"
        )[:90]
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=wnba_callback(
                        "game", str(game.get("event_id") or "")
                    ),
                )
            ]
        )
    navigation = []
    if normalized > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀ Prev",
                callback_data=wnba_callback("page", str(normalized - 1)),
            )
        )
    if normalized + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                "Next ▶",
                callback_data=wnba_callback("page", str(normalized + 1)),
            )
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(rows)


def wnba_game_text(game: dict) -> str:
    def value(key: str) -> str:
        raw = game.get(key)
        return "nodata" if raw in ("", None) else str(raw)

    return "\n".join(
        [
            f"🏀 {_wnba_matchup(game)}",
            str(game.get("commence_time_et") or ""),
            "",
            "Full game",
            (
                f"Spread: {game.get('away_team', '')} "
                f"{value('latest_away_spread')} "
                f"({value('latest_away_spread_price')}) · "
                f"{game.get('home_team', '')} "
                f"{value('latest_home_spread')} "
                f"({value('latest_home_spread_price')})"
            ),
            (
                f"Moneyline: {game.get('away_team', '')} "
                f"{value('latest_away_moneyline')} · "
                f"{game.get('home_team', '')} "
                f"{value('latest_home_moneyline')}"
            ),
            (
                f"Total: {value('latest_total')} "
                f"(O {value('latest_over_price')} / "
                f"U {value('latest_under_price')})"
            ),
            "",
            "First half",
            (
                f"Spread: {game.get('away_team', '')} "
                f"{value('latest_first_half_away_spread')} "
                f"({value('latest_first_half_away_spread_price')}) · "
                f"{game.get('home_team', '')} "
                f"{value('latest_first_half_home_spread')} "
                f"({value('latest_first_half_home_spread_price')})"
            ),
            (
                f"Total: {value('latest_first_half_total')} "
                f"(O {value('latest_first_half_over_price')} / "
                f"U {value('latest_first_half_under_price')})"
            ),
        ]
    )


def wnba_history_text(
    result: dict,
    *,
    include_revisions: bool,
) -> str:
    game = result["game"]
    lines = [f"🏀 {_wnba_matchup(game)}"]
    active = result.get("active_revision")
    if active:
        lines.extend(
            [
                "",
                "Latest active Claude lean",
                (
                    "Full game: "
                    f"{active.get('full_game_side_selection', '')} "
                    f"({active.get('full_game_side_strength', '')}); "
                    f"{active.get('full_game_total_selection', '')} "
                    f"({active.get('full_game_total_strength', '')})"
                ),
                str(active.get("summary") or ""),
            ]
        )
    else:
        lines.extend(["", "No published active Claude lean."])
    lines.extend(["", "User thought / lean history"])
    thoughts = result.get("thoughts") or []
    if not thoughts:
        lines.append("No user thoughts.")
    for thought in thoughts[-20:]:
        lines.append(
            (
                f"{thought.get('submitted_at_et', '')} · "
                f"{thought.get('period', '')} "
                f"{thought.get('market', '')} "
                f"{thought.get('side', '')}\n"
                f"{str(thought.get('thought_text') or '')[:500]}"
            )
        )
    if include_revisions:
        lines.extend(["", "Superseded / deleted revisions"])
        revisions = [
            revision
            for revision in result.get("revision_history") or []
            if revision.get("effective_status") != "active"
        ]
        if not revisions:
            lines.append("No superseded or deleted revisions.")
        for revision in revisions[-20:]:
            lines.append(
                (
                    f"{revision.get('requested_at_et', '')} · "
                    f"{revision.get('operation', '')} · "
                    f"{revision.get('effective_status', '')}\n"
                    f"{str(revision.get('summary') or '')[:500]}"
                )
            )
    return "\n".join(lines)[:3900]


class Receptionist:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self.runner: AgentRunner | None = None
        self.wnba = WnbaHelperClient(config)
        self._deployment_lock = asyncio.Lock()
        self._deployment_tasks: set[asyncio.Task[None]] = set()
        self._deployment_process: asyncio.subprocess.Process | None = None

    async def post_init(self, application: Application) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.database.initialize(self.config.repositories)
        recovered_deliveries = self.database.recover_interrupted_deliveries()
        recovered_deployments = self.database.recover_interrupted_deployments()
        self.database.prune_events()
        self.runner = AgentRunner(self.config, self.database, application.bot)
        recovery_messages = await self.runner.recover_startup()
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
        application.job_queue.run_repeating(
            self._delivery_retry_poll, interval=60, first=30
        )
        await self._notify_deployment(application)
        systemd_notify("READY=1\nSTATUS=Telegram polling and agent worker ready")
        log.info(
            "Receptionist started; recovered_runs=%d "
            "recovered_deliveries=%d recovered_deployments=%d",
            len(recovery_messages),
            recovered_deliveries,
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
            "/recover — reconcile a stuck run and retry Telegram delivery\n"
            "/verbose [on|off] — toggle detailed resource updates\n"
            "/provider — show provider\n\n"
            "/approve — execute the only pending deployment request\n"
            "/deny — reject the only pending deployment request\n"
            "/deployments — show recent deployment requests\n\n"
            "/wnba — choose a game for Claude lean analysis\n"
            "/wnba_history — latest active lean and user history\n"
            "/wnba_revisions — superseded/deleted lean revisions\n"
            "/wnba_undo — undo the latest published lean\n"
            "/wnba_cancel — clear the selected WNBA game\n\n"
            "Deployment messages also include Approve and Deny buttons.\n\n"
            "A WNBA_LEAN_REQUEST_V1 template is validated and routed through "
            "the same WNBA generation path as Generate now. Any other text "
            "is passed unchanged as the next agent prompt."
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
            process_id = active.get("process_id")
            process_state = (
                "alive"
                if process_id and process_group_alive(int(process_id))
                else "missing"
            )
            lines.append(
                f"Active run: {active['id'][:8]} · PID {process_state}"
            )
        if last:
            delivery = last.get("delivery_status") or "none"
            lines.append(
                f"Last run: {last['id'][:8]} · {last['status']} "
                f"· delivery {delivery}"
            )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Recover worker", callback_data="run:recover")]]
        )
        await update.message.reply_text(
            "\n".join(lines), reply_markup=keyboard
        )

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        stopped = bool(self.runner and await self.runner.cancel_active())
        await update.message.reply_text(
            "Stopping the active run." if stopped else "No active run."
        )

    async def recover(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        assert self.runner is not None
        run_message = await self.runner.recover()
        deploy_message = await self._recover_stale_deployment(
            update.effective_user.id
        )
        await update.message.reply_text(
            "\n\n".join(filter(None, (run_message, deploy_message)))
        )

    async def recover_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer("Checking the worker…")
        assert self.runner is not None
        run_message = await self.runner.recover()
        deploy_message = await self._recover_stale_deployment(
            update.effective_user.id
        )
        await query.message.reply_text(
            "\n\n".join(filter(None, (run_message, deploy_message)))
        )

    async def _recover_stale_deployment(self, user_id: int) -> str:
        failed = self.database.latest_head_changed_deployment(user_id)
        if failed is None:
            return ""
        repository = Path(failed["repository_path"]).resolve()
        if not repository.is_relative_to(self.config.repo_root):
            return "Deployment recovery refused: repository is outside the workspace."
        try:
            revision = await self._verified_git_revision(repository)
        except RuntimeError as error:
            return f"Deployment recovery refused: {error}"
        existing = self.database.equivalent_pending_deployment(
            user_id=user_id,
            repository_path=str(repository),
            revision=revision,
            command=failed["command"],
        )
        if existing:
            return (
                f"Recovered deployment request already pending: "
                f"{existing['id'][:8]} at {revision[:8]}."
            )
        now = datetime.now(UTC)
        request_id = str(uuid.uuid4())
        created = self.database.import_deployment_request(
            request_id=request_id,
            user_id=user_id,
            repository_path=str(repository),
            revision=revision,
            command=failed["command"],
            summary=failed["summary"],
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=15)).isoformat(),
            recovered_from_id=failed["id"],
        )
        if not created:
            return "Deployment recovery could not create a replacement request."
        return (
            f"Created replacement deployment request {request_id[:8]} at "
            f"verified current revision {revision[:8]}. Fresh approval is required."
        )

    async def _verified_git_revision(self, repository: Path) -> str:
        async def git(*arguments: str) -> str:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/git",
                "-c",
                f"safe.directory={repository}",
                "-C",
                str(repository),
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or "read-only Git verification failed")
            return stdout.decode("utf-8", errors="replace").strip()

        if await git("status", "--porcelain"):
            raise RuntimeError("working tree is not clean")
        revision = await git("rev-parse", "HEAD")
        upstream = await git("rev-parse", "@{upstream}")
        if revision != upstream:
            raise RuntimeError("HEAD does not match its configured upstream")
        return revision

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
        self_deployment = SELF_DEPLOY_WORKER in request["command"]
        if self_deployment:
            self._set_deployment_drain(request["id"])
            if self.database.active_run() or self.database.queued_count():
                self._clear_deployment_drain()
                return (
                    False,
                    "Receptionist deployment requires an idle queue. "
                    "Wait for active and queued runs to finish, then approve again.",
                )
        if not self.database.approve_deployment_request(request["id"]):
            if self_deployment:
                self._clear_deployment_drain()
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

    async def wnba_games(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            result = await self.wnba.request({"action": "list_games"})
        except Exception as error:
            log.warning("WNBA game lookup failed: %s", error)
            await update.message.reply_text(
                "❌ Could not load WNBA games. Try again."
            )
            return
        games = result.get("games") or []
        await update.message.reply_text(
            wnba_games_header(games, 0),
            reply_markup=wnba_games_markup(games, 0),
        )

    async def wnba_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cancelled = self.database.cancel_wnba_selection(
            update.effective_user.id
        )
        await update.message.reply_text(
            "WNBA selection cancelled."
            if cancelled
            else "No active WNBA selection."
        )

    async def _wnba_history(
        self,
        message,
        *,
        user_id: int,
        include_revisions: bool,
    ) -> None:
        selection = self.database.get_wnba_selection(user_id)
        if selection is None:
            await message.reply_text(
                "Select a current game with /wnba first."
            )
            return
        try:
            result = await self.wnba.request(
                {
                    "action": "history",
                    "event_id": selection["event_id"],
                }
            )
        except Exception as error:
            log.warning("WNBA history lookup failed: %s", error)
            await message.reply_text(
                "❌ Could not load WNBA history. Try again."
            )
            return
        rows = []
        if not include_revisions:
            rows.append(
                [
                    InlineKeyboardButton(
                        "Superseded / deleted",
                        callback_data=wnba_callback("revisions"),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "Undo latest",
                    callback_data=wnba_callback("undo"),
                )
            ]
        )
        await message.reply_text(
            wnba_history_text(
                result, include_revisions=include_revisions
            ),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def wnba_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._wnba_history(
            update.message,
            user_id=update.effective_user.id,
            include_revisions=False,
        )

    async def wnba_revisions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._wnba_history(
            update.message,
            user_id=update.effective_user.id,
            include_revisions=True,
        )

    async def _enqueue_prompt(
        self,
        message,
        *,
        user_id: int,
        chat_id: int,
        prompt: str,
        acknowledgement: str,
    ) -> None:
        if self._deployment_drain_path.exists():
            await message.reply_text(
                "⏸️ Receptionist deployment is still completing. "
                "Wait for the final 🚀 deployment notification, then resend."
            )
            return
        if self.database.queued_count() >= self.config.max_queued_messages:
            await message.reply_text(
                f"Queue is full ({self.config.max_queued_messages} messages)."
            )
            return
        session = self.database.get_or_create_active_session(user_id)
        run = self.database.enqueue_run(session["id"], chat_id, prompt)
        position = self.database.queued_count()
        await message.reply_text(
            f"{acknowledgement} as run {run['id'][:8]} "
            f"({session['repository_name']}, queue position {position})."
        )
        assert self.runner is not None
        self.runner.notify()

    async def _queue_wnba_action(
        self,
        message,
        *,
        user_id: int,
        chat_id: int,
        action: str,
    ) -> None:
        selection = self.database.get_wnba_selection(user_id)
        if selection is None:
            await message.reply_text(
                "Select a current game with /wnba first."
            )
            return
        try:
            result = await self.wnba.request(
                {
                    "action": action,
                    "event_id": selection["event_id"],
                    "matchup": selection["matchup"],
                }
            )
        except Exception as error:
            log.warning("WNBA generation request failed: %s", error)
            await message.reply_text(
                "❌ WNBA game validation failed. Nothing was queued."
            )
            return
        await self._enqueue_prompt(
            message,
            user_id=user_id,
            chat_id=chat_id,
            prompt=result["skill_prompt"],
            acknowledgement=(
                "↩️ WNBA undo queued"
                if action == "build_undo"
                else "🏀 WNBA lean generation queued"
            ),
        )

    async def wnba_undo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._queue_wnba_action(
            update.message,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            action="build_undo",
        )

    async def wnba_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        data = str(query.data or "")
        parts = data.split(":", 2)
        if len(parts) < 2:
            await query.answer("Invalid WNBA action.", alert=True)
            return
        action = parts[1]
        user_id = update.effective_user.id
        try:
            if action == "page" and len(parts) == 3:
                try:
                    target_page = int(parts[2])
                except ValueError:
                    await query.answer("Invalid WNBA action.", alert=True)
                    return
                result = await self.wnba.request({"action": "list_games"})
                games = result.get("games") or []
                await query.edit_message_text(
                    wnba_games_header(games, target_page),
                    reply_markup=wnba_games_markup(games, target_page),
                )
                await query.answer()
                return
            if action == "game" and len(parts) == 3:
                result = await self.wnba.request(
                    {"action": "game", "event_id": parts[2]}
                )
                game = result["game"]
                self.database.set_wnba_selection(
                    user_id=user_id,
                    event_id=str(game["event_id"]),
                    matchup=_wnba_matchup(game),
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Generate now",
                                callback_data=wnba_callback("generate"),
                            ),
                            InlineKeyboardButton(
                                "Copy template text",
                                callback_data=wnba_callback("copy"),
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "View lean history",
                                callback_data=wnba_callback("history"),
                            )
                        ],
                    ]
                )
                await query.edit_message_text(
                    wnba_game_text(game),
                    reply_markup=keyboard,
                )
                await query.answer()
                return
            if action == "generate":
                await query.answer("Queuing WNBA generation…")
                await self._queue_wnba_action(
                    query.message,
                    user_id=user_id,
                    chat_id=update.effective_chat.id,
                    action="build_generation",
                )
                return
            if action == "copy":
                selection = self.database.get_wnba_selection(user_id)
                if selection is None:
                    await query.answer(
                        "Selection expired. Run /wnba again.",
                        alert=True,
                    )
                    return
                result = await self.wnba.request(
                    {
                        "action": "build_generation",
                        "event_id": selection["event_id"],
                        "matchup": selection["matchup"],
                    }
                )
                await query.answer()
                await query.message.reply_text(result["template"])
                return
            if action in {"history", "revisions"}:
                await query.answer()
                await self._wnba_history(
                    query.message,
                    user_id=user_id,
                    include_revisions=action == "revisions",
                )
                return
            if action == "undo":
                await query.answer("Queuing WNBA undo…")
                await self._queue_wnba_action(
                    query.message,
                    user_id=user_id,
                    chat_id=update.effective_chat.id,
                    action="build_undo",
                )
                return
            await query.answer("Invalid WNBA action.", alert=True)
        except Exception as error:
            log.warning("WNBA callback failed: %s", error)
            await query.answer(
                "WNBA action failed. Try again.", alert=True
            )

    async def exact_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        if message is None or message.text is None:
            return
        prompt = message.text
        acknowledgement = "👀 Received exactly"
        if prompt.startswith(f"{WNBA_TEMPLATE_HEADER}\n"):
            try:
                result = await self.wnba.request(
                    {"action": "validate_template", "template": prompt}
                )
            except Exception as error:
                log.warning("WNBA template validation failed: %s", error)
                await message.reply_text(
                    "❌ WNBA template validation failed. Nothing was queued."
                )
                return
            game = result["game"]
            self.database.set_wnba_selection(
                user_id=update.effective_user.id,
                event_id=str(game["event_id"]),
                matchup=_wnba_matchup(game),
            )
            prompt = result["skill_prompt"]
            acknowledgement = "🏀 WNBA lean generation queued"
        await self._enqueue_prompt(
            message,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            prompt=prompt,
            acknowledgement=acknowledgement,
        )

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

    async def _delivery_retry_poll(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self.runner:
            await self.runner.retry_pending_deliveries()

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
            self_deployment = SELF_DEPLOY_WORKER in request["command"]
            if status == "failed" and self_deployment:
                self._clear_deployment_drain()
            if status == "succeeded" and self_deployment:
                message = (
                    f"⏳ Receptionist deployment {request['id'][:8]} launched. "
                    "New prompts are paused until the final 🚀 deployment "
                    "notification."
                )
            else:
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
        self._clear_deployment_drain()

    @property
    def _deployment_drain_path(self) -> Path:
        return self.config.state_dir / "deployment-drain.json"

    def _set_deployment_drain(self, request_id: str) -> None:
        path = self._deployment_drain_path
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _clear_deployment_drain(self) -> None:
        self._deployment_drain_path.unlink(missing_ok=True)


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
        ("recover", receptionist.recover),
        ("approve", receptionist.approve),
        ("deny", receptionist.deny),
        ("deployments", receptionist.deployments),
        ("wnba", receptionist.wnba_games),
        ("wnba_history", receptionist.wnba_history),
        ("wnba_revisions", receptionist.wnba_revisions),
        ("wnba_undo", receptionist.wnba_undo),
        ("wnba_cancel", receptionist.wnba_cancel),
    ]
    for name, handler in commands:
        application.add_handler(
            CommandHandler(name, receptionist.authorized(handler))
        )
    application.add_handler(
        CallbackQueryHandler(
            receptionist.authorized(receptionist.recover_button),
            pattern=r"^run:recover$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            receptionist.authorized(receptionist.deployment_button),
            pattern=r"^deploy:(approve|deny):[0-9a-f-]{36}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            receptionist.authorized(receptionist.wnba_button),
            pattern=WNBA_CALLBACK_PATTERN,
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
