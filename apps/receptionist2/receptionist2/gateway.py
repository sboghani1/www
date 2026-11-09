from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import EFFORT_LEVELS, GatewayConfig, resolve_model
from .context import build_context_manifest
from .database import Database
from .deployments import (
    DIAGNOSE_DEPLOYMENT_TEXT,
    diagnose_markup,
    diagnosis_prompt,
)
from .images import (
    ALBUM_SETTLE_SECONDS,
    IMAGE_MAX_BYTES,
    TelegramImage,
    image_prompt,
    image_summary,
    telegram_image,
)
from .investigation import investigation_prompt
from .nfl_audio import audio_review_prompt, remove_audio_files, telegram_audio
from .resources import resource_summary
from .scheduling import next_run
from .knowledge import knowledge_transfer_prompt
from .tombstone import (
    continuation_name,
    resume_prompt,
    tombstone_path,
    write_prompt,
)
from .voice import transcript_preview, voice_prompt

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("receptionist2.gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)
RESTART_PATTERN = re.compile(r"^r2:restart:(?:now|10m|wait|cancel)$")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ROLLOVER_PATTERN = re.compile(rf"^session:(?:new|continue):{UUID}$")
DIAGNOSE_PATTERN = re.compile(rf"^deploy:diagnose:{UUID}$")
RETRY_PATTERN = re.compile(rf"^r2:retry:{UUID}$")
NEW_CHAT_CALLBACK = "r2:newchat"
VOICE_PATTERN = re.compile(rf"^r2:voice:(?:run|trash):{UUID}$")
TOMB_PATTERN = re.compile(rf"^r2:tomb:(?:new|del|delyes|keep):{UUID}$")
# The ☰ menu. Every command still works when typed; these are the common ones.
PRIVATE_MENU = (
    ("new", "Start a new conversation"),
    ("tombstone", "Hand this chat off to a fresh one"),
    ("knowledge_transfer", "Save reusable learnings to the knowledge base"),
    ("instructions", "Standing instructions for this chat"),
    ("chats", "Chat topics in the group"),
    ("sessions", "List conversations"),
    ("switch", "Switch conversation"),
    ("status", "Queue, worker, and session status"),
    ("stop", "Stop the active run"),
    ("model", "Model for new runs"),
    ("effort", "Effort for new runs"),
    ("topics", "Recent topics and context usage"),
    ("recover", "Reconcile stuck work"),
    ("investigate", "Structured investigation run"),
    ("schedules", "Recurring tasks"),
    ("help", "All commands"),
)
GROUP_MENU = (
    ("tombstone", "Hand this chat off to a fresh one"),
    ("knowledge_transfer", "Save reusable learnings to the knowledge base"),
    ("instructions", "Standing instructions for this chat"),
    ("new", "Open a new chat: /new <name>"),
    ("chats", "Every chat and its state"),
    ("status", "Status of this chat"),
    ("stop", "Stop this chat's run"),
    ("model", "Model for this chat"),
    ("effort", "Effort for this chat"),
    ("help", "All commands"),
)
TRANSCRIBE_TIMEOUT_SECONDS = 15 * 60
INSTRUCTIONS_MAX_CHARS = 3500
HUB_NAME = "📋 Chats"
FRUITS = (
    "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓",
    "🫐", "🍈", "🍒", "🍑", "🥭", "🍍", "🥥", "🥝",
)
# Telegram allows 128 characters; leave room for the fruit.
TOPIC_NAME_MAX_CHARS = 120
NAME_PROMPT = "Name for the new chat?"
NAME_FIRST_TEXT = (
    "Name the chat first: type its name here or tap ➕, then send it inside "
    "the new topic."
)
HUB_TEXT = (
    "📋 Chats\n\n"
    "Each topic in this group is its own Receptionist2 conversation, and up "
    "to two of them work at once. To open one, type its name here (or tap "
    "➕), then write inside the new topic. Close a topic to archive it.\n\n"
    "Voice notes and screenshots work in any topic. In a topic: /stop, "
    "/status, /model, /effort, /instructions. Anywhere: /chats, /new <name>."
)
# No set-gid bit: the gateway unit's RestrictSUIDSGID=true rejects it with
# EPERM. Its Group=receptionist2-state already gives new files the shared
# group the agent reads them through.
SHARED_DIR_MODE = 0o770
RECOVER_MENU_TEXT = "♻️ Recover"
TOPICS_MENU_TEXT = "🧭 Topics & Context"
STOP_MENU_TEXT = "🛑 Stop"
STATUS_MENU_TEXT = "📋 Status"
RUN_ICONS = {
    "succeeded": "✅",
    "failed": "❌",
    "cancelled": "🛑",
    "interrupted": "⚠️",
}
HELP_TEXT = (
    "/new [name] — start a new conversation\n"
    "/sessions — list conversations\n"
    "/switch <id-prefix> — switch conversation\n"
    "/reset — archive this conversation and start fresh\n"
    "/topics — recent topics and active context usage\n"
    "/status — queue, worker, and session status\n"
    "/stop — stop the active run\n"
    "/recover — reconcile stuck work and retry deliveries\n"
    "/verbose [on|off] — toggle detailed progress updates\n"
    "/model [alias-or-id], /effort [level] — runtime for new runs\n"
    "/investigate <problem> — structured investigation run\n"
    "/nfl_audio <season> <week> — collect private NFL voice notes\n"
    "/nfl_audio_done — transcribe and build a review-only slate\n"
    "/nfl_audio_cancel — discard the active audio collection\n"
    "/deployments — recent deployment requests\n"
    "/restart — drain and restart the gateway and worker\n"
    "/schedules, /schedule_add, /schedule_pause, /schedule_resume, "
    "/schedule_run — recurring tasks\n\n"
    "Validated immutable deployment requests execute automatically; "
    "Telegram reports their exact revision, command, and result.\n\n"
    "Photos and image files (screenshots work well; send several as one "
    "album) go to the agent with their caption as the request.\n\n"
    "Voice notes and audio files are transcribed on the server; tap Run to "
    "send the transcript to this chat's agent or Trash to delete it.\n\n"
    "/instructions [text|skill path|clear] — standing instructions every run "
    "in this chat follows\n"
    "/tombstone — write everything about this chat to a file, then continue "
    "in a fresh chat that starts from it and delete this one\n"
    "/knowledge_transfer [focus] — record this chat's reusable learnings in "
    "www/knowledge_base\n\n"
    "Any other text is passed unchanged as the next agent prompt.\n\n"
    "/chats — every chat topic in the Receptionist2 group and its state. "
    "In the group, each topic is its own conversation: type a name in "
    "📋 Chats, tap ➕, or send /new <name> to open one, and use /stop, "
    "/status, /model, /effort inside it."
)


def topic_link(chat_id: int, thread_id: int) -> str:
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{thread_id}"


def clean_name(text: str) -> str:
    """A chat name from user input: one line, at most TOPIC_NAME_MAX_CHARS."""
    line = " ".join(text.split())
    if len(line) > TOPIC_NAME_MAX_CHARS:
        line = line[:TOPIC_NAME_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return line


def has_fruit(name: str) -> bool:
    return name.startswith(FRUITS)


def fruit_name(name: str, taken: set[str] = frozenset()) -> str:
    """Prefix a random fruit, preferring one no open chat already uses."""
    if has_fruit(name):
        return name
    free = [fruit for fruit in FRUITS if fruit not in taken] or list(FRUITS)
    return f"{random.choice(free)} {name}"


def thread_of(message) -> int | None:
    """The forum topic a message belongs to; None for General or a DM."""
    if message is None or not getattr(message, "is_topic_message", False):
        return None
    return message.message_thread_id


def chats_text(sessions: list[dict], chat_id: int) -> str:
    if not sessions:
        return "No chat topics yet."
    lines = ["Chats (newest first):"]
    for session in sessions[:30]:
        state = (
            "closed"
            if session["status"] != "active"
            else session.get("last_status") or "new"
        )
        lines.append(
            f"• {session['display_name']} — {state}\n  "
            + topic_link(
                int(session["telegram_chat_id"] or chat_id),
                int(session["telegram_thread_id"]),
            )
        )
    return "\n".join(lines)


def recovery_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [RECOVER_MENU_TEXT, DIAGNOSE_DEPLOYMENT_TEXT],
            [TOPICS_MENU_TEXT, STOP_MENU_TEXT],
            [STATUS_MENU_TEXT],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def session_topics_text(session_name: str, context: dict) -> str:
    lines = [f"🧭 Current session: {session_name}", "", "Recent topics"]
    topics = context["recent_topics"]
    if topics:
        for index, terms in enumerate(topics, start=1):
            lines.append(f"{index}. {' · '.join(terms)}")
    else:
        lines.append("(none recorded yet)")
    context_tokens = context["context_tokens"]
    context_window_tokens = context["context_window_tokens"]
    lines.extend(["", f"Successful runs tracked: {context['successful_runs']}"])
    if context_tokens is None:
        lines.append("Active context: waiting for Claude usage data")
    elif context_window_tokens:
        percent = round(100 * context_tokens / context_window_tokens)
        lines.append(
            f"Active context: {context_tokens:,} / "
            f"{context_window_tokens:,} tokens ({percent}%)"
        )
    else:
        lines.append(f"Active context: approximately {context_tokens:,} tokens")
    return "\n".join(lines)


def markup_from_json(raw: str | None) -> InlineKeyboardMarkup | None:
    if not raw:
        return None
    try:
        rows = json.loads(raw)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        str(button["text"]),
                        callback_data=str(button["callback_data"]),
                    )
                    for button in row
                ]
                for row in rows
            ]
        )
    except (TypeError, KeyError, ValueError):
        log.warning("Ignored malformed outbox reply markup")
        return None


def progress_text(run: dict, *, verbose: bool, now: datetime) -> str:
    started = datetime.fromisoformat(run["started_at"])
    elapsed = max(0, int((now - started).total_seconds()))
    lines = [
        f"⏳ Run {run['id'][:8]} · {run['model']} / {run['effort']}",
        run.get("activity") or "Starting agent",
        f"Current: {run.get('current_work') or 'Preparing Claude'}",
        f"Elapsed: {elapsed // 60}m {elapsed % 60}s",
    ]
    if run.get("last_event_at"):
        age = max(
            0,
            int((now - datetime.fromisoformat(run["last_event_at"])).total_seconds()),
        )
        lines.append(f"Last output: {age}s ago")
    if verbose:
        lines.append(resource_summary(run.get("process_id")))
    return "\n".join(lines)


class Gateway:
    def __init__(self, config: GatewayConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self._progress_edits: dict[str, float] = {}
        self._albums: dict[str, dict] = {}
        self._refused_albums: set[str] = set()
        # Whisper is CPU-heavy: transcribe one voice note at a time.
        self._transcribe_lock = asyncio.Semaphore(1)
        self._voice_tasks: set[asyncio.Task] = set()
        self._downloading: set[str] = set()

    async def post_init(self, application: Application) -> None:
        self.database.initialize()
        self.database.recover_sending_outbox()
        self.database.set_state("gateway_status", "ready")
        application.job_queue.run_repeating(
            self.deliver_outbox,
            interval=self.config.delivery_poll_seconds,
            first=1,
        )
        application.job_queue.run_repeating(
            self.enqueue_due_schedules,
            interval=self.config.schedule_poll_seconds,
            first=3,
        )
        application.job_queue.run_repeating(
            self.update_progress, interval=10, first=2
        )
        for note in self.database.transcribing_voice_notes():
            self._start_transcription(application.bot, note)
        await self._set_menus(application.bot)
        if self.config.group_chat_id:
            try:
                await self.ensure_hub(application.bot)
            except TelegramError as error:
                log.warning(
                    "Could not set up the chats hub (is the bot an admin "
                    "with Manage Topics and Pin Messages?): %s",
                    error,
                )

    async def _set_menus(self, bot) -> None:
        try:
            await bot.set_my_commands(
                [BotCommand(*item) for item in PRIVATE_MENU],
                scope=BotCommandScopeAllPrivateChats(),
            )
            if self.config.group_chat_id:
                await bot.set_my_commands(
                    [BotCommand(*item) for item in GROUP_MENU],
                    scope=BotCommandScopeChat(self.config.group_chat_id),
                )
        except TelegramError as error:
            log.warning("Could not set the command menu: %s", error)

    def hub_thread(self) -> int | None:
        value = self.database.get_state(
            f"group_hub_thread:{self.config.group_chat_id}", ""
        )
        return int(value) if value else None

    async def ensure_hub(self, bot) -> int:
        """The 📋 Chats topic with a pinned ➕, created once per group."""
        existing = self.hub_thread()
        if existing:
            await self._refresh_hub_text(bot)
            return existing
        chat_id = self.config.group_chat_id
        topic = await bot.create_forum_topic(chat_id, HUB_NAME)
        thread = topic.message_thread_id
        self.database.set_state(f"group_hub_thread:{chat_id}", str(thread))
        sent = await bot.send_message(
            chat_id,
            HUB_TEXT,
            message_thread_id=thread,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕", callback_data=NEW_CHAT_CALLBACK)]]
            ),
        )
        self.database.set_state(
            f"group_hub_message:{chat_id}", str(sent.message_id)
        )
        try:
            await bot.pin_chat_message(
                chat_id, sent.message_id, disable_notification=True
            )
        except TelegramError as error:
            log.warning("Could not pin the chats hub message: %s", error)
        try:
            await bot.hide_general_forum_topic(chat_id)
        except TelegramError as error:
            log.info("General topic not hidden: %s", error)
        return thread

    def _fruit_name(self, user_id: int, name: str) -> str:
        taken = {
            row["display_name"][: len(fruit)]
            for row in self.database.topic_sessions(user_id)
            if row["status"] == "active"
            for fruit in FRUITS
            if row["display_name"].startswith(fruit)
        }
        return fruit_name(name, taken)

    async def _refresh_hub_text(self, bot) -> None:
        """Keep the pinned hub message's wording current across releases."""
        # getChat does not report pins inside forum topics, so the id is kept.
        message_id = self.database.get_state(
            f"group_hub_message:{self.config.group_chat_id}", ""
        )
        if not message_id:
            return
        try:
            await bot.edit_message_text(
                HUB_TEXT,
                chat_id=self.config.group_chat_id,
                message_id=int(message_id),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("➕", callback_data=NEW_CHAT_CALLBACK)]]
                ),
            )
        except BadRequest as error:
            if "not modified" not in str(error).lower():
                raise

    async def open_topic(
        self, bot, *, user_id: int, name: str, greeting: str | None = None
    ) -> dict:
        """A new chat topic named with a fruit and the user's name for it."""
        chat_id = self.config.group_chat_id
        name = self._fruit_name(user_id, name)
        topic = await bot.create_forum_topic(chat_id, name)
        session = self.database.create_topic_session(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=topic.message_thread_id,
            name=name,
            model=self.config.default_model,
            effort=self.config.default_effort,
        )
        try:
            await bot.send_message(
                chat_id,
                greeting or f"{name} is ready. Send your first message here.",
                message_thread_id=topic.message_thread_id,
            )
        except TelegramError as error:
            log.warning("Could not greet the new topic: %s", error)
        return session

    async def adopt_topic(
        self, bot, user_id: int, thread_id: int, name: str
    ) -> dict:
        """Register a topic the user created in Telegram, adding a fruit."""
        named = self._fruit_name(user_id, name)
        if named != name:
            try:
                await bot.edit_forum_topic(
                    self.config.group_chat_id, thread_id, name=named
                )
            except TelegramError as error:
                log.warning("Could not rename topic %s: %s", thread_id, error)
                named = name
        return self.database.create_topic_session(
            user_id=user_id,
            chat_id=self.config.group_chat_id,
            thread_id=thread_id,
            name=named,
            model=self.config.default_model,
            effort=self.config.default_effort,
        )

    async def name_new_chat(self, message, user_id: int, text: str) -> None:
        name = clean_name(text)
        if not name:
            await message.reply_text(NAME_PROMPT)
            return
        try:
            session = await self.open_topic(
                message.get_bot(), user_id=user_id, name=name
            )
        except TelegramError as error:
            await message.reply_text(f"Could not open a topic: {error}")
            return
        await message.reply_text(
            f"💬 Opened {session['display_name']}: "
            + topic_link(
                self.config.group_chat_id, int(session["telegram_thread_id"])
            ),
            disable_web_page_preview=True,
        )

    def is_hub(self, thread: int | None) -> bool:
        return thread is None or thread == self.hub_thread()

    def context_session(self, update: Update) -> dict | None:
        """The topic's session in the group, else the private chat's."""
        if not self.in_group(update):
            return self.database.active_session(update.effective_user.id)
        thread = thread_of(update.effective_message)
        if thread is None or thread == self.hub_thread():
            return None
        return self.database.topic_session(self.config.group_chat_id, thread)

    async def _topic_only(self, update: Update, command: str) -> None:
        await update.message.reply_text(
            f"Use {command} inside a chat topic, or in the private chat."
        )

    async def new_chat_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        try:
            # Any text written in the hub names a new chat; this just asks.
            await context.bot.send_message(
                self.config.group_chat_id,
                NAME_PROMPT,
                message_thread_id=thread_of(query.message),
                reply_markup=ForceReply(input_field_placeholder="Chat name"),
            )
        except TelegramError as error:
            log.warning("Could not ask for the new chat name: %s", error)

    async def chats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.config.group_chat_id:
            await update.message.reply_text(
                "No Receptionist2 group is configured "
                "(RECEPTIONIST2_GROUP_CHAT_ID)."
            )
            return
        await update.message.reply_text(
            chats_text(
                self.database.topic_sessions(update.effective_user.id),
                self.config.group_chat_id,
            ),
            disable_web_page_preview=True,
        )

    async def topic_event(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        thread = message.message_thread_id
        if not thread or thread == self.hub_thread():
            return
        chat_id = self.config.group_chat_id
        if message.forum_topic_created:
            await self.adopt_topic(
                context.bot,
                update.effective_user.id,
                thread,
                message.forum_topic_created.name,
            )
        elif message.forum_topic_edited and message.forum_topic_edited.name:
            session = self.database.topic_session(chat_id, thread)
            if session:
                self.database.set_session_title(
                    session["id"], message.forum_topic_edited.name
                )
        elif message.forum_topic_closed:
            self.database.set_topic_open(chat_id, thread, False)
        elif message.forum_topic_reopened:
            self.database.set_topic_open(chat_id, thread, True)

    async def post_shutdown(self, application: Application) -> None:
        self.database.set_state("gateway_status", "stopped")

    def in_group(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(
            self.config.group_chat_id
            and chat is not None
            and chat.id == self.config.group_chat_id
        )

    def authorized(self, handler, *, group: bool = False):
        async def wrapped(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            user = update.effective_user
            chat = update.effective_chat
            in_group = self.in_group(update)
            if (
                user is None
                or chat is None
                or not (chat.type == ChatType.PRIVATE or in_group)
                or user.id != self.config.allowed_user_id
            ):
                log.warning(
                    "Rejected update user=%s chat=%s",
                    user.id if user else None,
                    chat.type if chat else None,
                )
                return
            if in_group:
                if group:
                    await handler(update, context)
                elif update.callback_query:
                    await update.callback_query.answer(
                        "That works only in the private chat."
                    )
                elif update.message:
                    await update.message.reply_text(
                        "That works only in the private chat with me."
                    )
                return
            self.database.ensure_user(
                user.id,
                chat.id,
                default_model=self.config.default_model,
                default_effort=self.config.default_effort,
            )
            await handler(update, context)

        return wrapped

    async def start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        manifest = build_context_manifest(
            self.database,
            user_id=update.effective_user.id,
            repo_root=self.config.repo_root,
            timezone=str(self.config.timezone),
        )
        missing = [
            key
            for key, value in manifest["sources"].items()
            if value.get("status") != "loaded"
        ]
        if missing:
            await update.message.reply_text(
                "❌ Receptionist2 is not ready. Missing context: "
                + ", ".join(missing)
            )
            return
        await update.message.reply_text(
            "✅ Receptionist2 ready\n"
            f"{len(manifest['repositories'])} repositories · "
            f"{len(manifest['schedules'])} recurring schedules · "
            f"timezone {manifest['timezone']}\n\n"
            "Send plain text to run an exact agent turn. Use /help for "
            "commands.",
            reply_markup=recovery_menu(),
        )

    async def help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(
            HELP_TEXT,
            # The recovery keyboard's buttons work only in the private chat.
            reply_markup=None if self.in_group(update) else recovery_menu(),
        )

    async def nfl_audio_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /nfl_audio <season> <week>")
            return
        try:
            season, week = (int(value) for value in context.args)
        except ValueError:
            await update.message.reply_text(
                "Season and week must both be numbers."
            )
            return
        if season < 2020 or season > 2100 or week < 1 or week > 22:
            await update.message.reply_text(
                "Use an NFL season from 2020–2100 and a week from 1–22."
            )
            return
        collection, created = self.database.start_nfl_audio_collection(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            season=season,
            week=week,
            default_model=self.config.default_model,
            default_effort=self.config.default_effort,
        )
        if not created:
            count = len(self.database.nfl_audio_items(collection["id"]))
            await update.message.reply_text(
                "An NFL audio collection is already active for "
                f"{collection['season']} Week {collection['week']} with "
                f"{count} clip{'s' if count != 1 else ''}. "
                "Use /nfl_audio_done or /nfl_audio_cancel first."
            )
            return
        await update.message.reply_text(
            f"🎙️ Collecting your {season} Week {week} NFL commentary.\n"
            "Send one or more Telegram voice notes or audio files, then use "
            "/nfl_audio_done. Nothing will become an official pick without a "
            "separate approval."
        )

    async def nfl_audio_receive(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        selected = telegram_audio(message)
        if selected is None:
            await message.reply_text(
                "That document is not an audio file. Send a voice note or "
                "audio file."
            )
            return
        collection = self.database.active_nfl_audio_collection(
            update.effective_user.id, update.effective_chat.id
        )
        if collection is None:
            await message.reply_text(
                "Start a collection first with /nfl_audio <season> <week>."
            )
            return
        existing = self.database.nfl_audio_item_by_message(
            update.effective_chat.id, message.message_id
        )
        if existing:
            count = len(self.database.nfl_audio_items(collection["id"]))
            await message.reply_text(
                f"Already saved that clip · {count} total."
            )
            return
        items = self.database.nfl_audio_items(collection["id"])
        if len(items) >= self.config.nfl_audio_max_clips:
            await message.reply_text(
                f"This collection already has the maximum "
                f"{self.config.nfl_audio_max_clips} clips."
            )
            return
        if (
            selected.duration_seconds is not None
            and selected.duration_seconds
            > self.config.nfl_audio_max_duration_seconds
        ):
            await message.reply_text(
                "That clip is too long. The per-clip limit is "
                f"{self.config.nfl_audio_max_duration_seconds // 60} minutes."
            )
            return
        reported_size = selected.media.file_size
        if (
            reported_size is not None
            and reported_size > self.config.nfl_audio_max_bytes
        ):
            await message.reply_text(
                "That clip is too large. The download limit is "
                f"{self.config.nfl_audio_max_bytes // (1024 * 1024)} MB."
            )
            return
        collection_dir = self.config.nfl_audio_dir / collection["id"]
        final_path: Path | None = None
        temp_path: Path | None = None
        stored = False
        try:
            collection_dir.mkdir(
                parents=True, exist_ok=True, mode=SHARED_DIR_MODE
            )
            collection_dir.chmod(SHARED_DIR_MODE)
            final_path = collection_dir / f"{uuid.uuid4()}{selected.suffix}"
            temp_path = collection_dir / f".{final_path.name}.part"
            telegram_file = await selected.media.get_file()
            await telegram_file.download_to_drive(custom_path=temp_path)
            actual_size = temp_path.stat().st_size
            if actual_size > self.config.nfl_audio_max_bytes:
                await message.reply_text(
                    "That clip exceeded the download size limit."
                )
                return
            temp_path.chmod(0o660)
            temp_path.replace(final_path)
            item, created = self.database.add_nfl_audio_item(
                collection_id=collection["id"],
                chat_id=update.effective_chat.id,
                message_id=message.message_id,
                kind=selected.kind,
                mime_type=selected.mime_type,
                local_path=str(final_path),
                duration_seconds=selected.duration_seconds,
                size_bytes=actual_size,
                max_items=self.config.nfl_audio_max_clips,
            )
            if not created:
                final_path.unlink(missing_ok=True)
            else:
                stored = True
            count = len(self.database.nfl_audio_items(collection["id"]))
            duration = item.get("duration_seconds")
            duration_text = (
                f" · {duration // 60}:{duration % 60:02d}"
                if duration is not None
                else ""
            )
            await message.reply_text(
                f"🎧 Saved clip {count}{duration_text}. "
                "Send another or use /nfl_audio_done."
            )
        except (OSError, RuntimeError, TelegramError, ValueError) as error:
            log.exception("Could not store NFL audio message")
            if final_path is not None and not stored:
                final_path.unlink(missing_ok=True)
            await message.reply_text(
                f"Could not save that audio clip: {error}"
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    async def nfl_audio_done(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        existing = self.database.inbound_run(
            update.effective_chat.id, update.effective_message.message_id
        )
        if existing:
            await update.message.reply_text(
                f"Already queued · run {existing['id'][:8]}."
            )
            return
        collection = self.database.active_nfl_audio_collection(
            update.effective_user.id, update.effective_chat.id
        )
        if collection is None:
            await update.message.reply_text(
                "There is no active NFL audio collection."
            )
            return
        items = self.database.nfl_audio_items(collection["id"])
        if not items:
            await update.message.reply_text(
                "Send at least one voice note or audio file first."
            )
            return
        prompt = audio_review_prompt(collection, items)
        try:
            run, created = self.database.queue_nfl_audio_collection(
                collection_id=collection["id"],
                message_id=update.effective_message.message_id,
                exact_prompt=prompt,
            )
        except (KeyError, RuntimeError, ValueError) as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"{'Queued' if created else 'Already queued'} {len(items)} "
            f"clip{'s' if len(items) != 1 else ''} for a review-only "
            f"{collection['season']} Week {collection['week']} analysis · "
            f"run {run['id'][:8]}.\n"
            "Raw audio will be removed when the run finishes."
        )

    async def nfl_audio_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        collection, paths = self.database.cancel_nfl_audio_collection(
            update.effective_user.id, update.effective_chat.id
        )
        if collection is None:
            await update.message.reply_text(
                "There is no active NFL audio collection."
            )
            return
        errors = remove_audio_files(paths)
        if not errors:
            self.database.mark_nfl_audio_collection_cleaned(collection["id"])
        await update.message.reply_text(
            f"Cancelled {collection['season']} Week {collection['week']} "
            f"audio collection."
            + (
                " Some temporary files could not be removed; maintenance "
                "will retry."
                if errors
                else ""
            )
        )

    async def topics(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.database.active_session(update.effective_user.id)
        await update.message.reply_text(
            session_topics_text(
                f"{session['display_name']} ({session['id'][:8]})",
                self.database.get_session_context(session["id"]),
            )
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

    async def stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self.in_group(update):
            session = self.context_session(update)
            if session is None:
                await self._topic_only(update, "/stop")
                return
            run = self.database.request_cancel(session["id"])
        else:
            run = self.database.request_cancel(private_only=True)
        await update.message.reply_text(
            f"🛑 Stopping run {run['id'][:8]}…" if run else "No active run here."
        )

    async def reset(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.database.reset_session(
            update.effective_user.id, update.effective_chat.id
        )
        await update.message.reply_text(
            f"Context reset. New session: {session['id'][:8]}"
        )

    def _recover_result(
        self, user_id: int
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        lines: list[str] = []
        heartbeat = self.database.get_state("worker_heartbeat", "")
        try:
            age = int(time.time() - float(heartbeat))
            lines.append(
                f"Worker heartbeat: {age}s ago"
                + (" ⚠️ stale — use the watchdog /restart" if age > 60 else "")
            )
        except ValueError:
            lines.append(
                "Worker heartbeat: missing ⚠️ — use the watchdog /restart"
            )
        running = self.database.running_runs()
        for run in running:
            lines.append(
                f"Run {run['id'][:8]} is running (PID {run['process_id']}); "
                "the worker reconciles it automatically. Use /stop to end it."
            )
        if not running:
            lines.append("No active run.")
        retried = self.database.retry_failed_outbox()
        if retried:
            lines.append(f"Retrying {retried} pending Telegram delivery item(s).")
        buttons: list[list[InlineKeyboardButton]] = []
        failed = self.database.latest_unsuccessful_run(user_id)
        if failed:
            lines.append(
                f"Latest run {failed['id'][:8]} {failed['status']}: "
                f"{(failed['error'] or '')[:300]}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Retry run {failed['id'][:8]}",
                        callback_data=f"r2:retry:{failed['id']}",
                    )
                ]
            )
        deployment = self.database.latest_deployment_request(user_id)
        if deployment and deployment["status"] == "failed":
            if deployment["diagnosis_run_id"]:
                lines.append(
                    "Deployment diagnosis already started as run "
                    f"{deployment['diagnosis_run_id'][:8]}."
                )
            else:
                lines.append(
                    f"Deployment {deployment['id'][:8]} failed and needs "
                    "diagnosis; it will not be replayed automatically."
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            DIAGNOSE_DEPLOYMENT_TEXT,
                            callback_data=f"deploy:diagnose:{deployment['id']}",
                        )
                    ]
                )
        return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None

    async def recover(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        text, markup = self._recover_result(update.effective_user.id)
        await update.message.reply_text(text, reply_markup=markup)

    async def retry_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        run_id = str(query.data).removeprefix("r2:retry:")
        try:
            run = self.database.requeue_run(update.effective_user.id, run_id)
        except LookupError as error:
            await query.answer(str(error), show_alert=True)
            return
        await query.answer("Queued")
        await query.message.reply_text(
            f"🔁 Retrying {run_id[:8]} as run {run['id'][:8]}."
        )

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
            status = (
                "queued" if request["status"] == "approved" else request["status"]
            )
            lines.append(
                f"{request['id'][:8]} · {status} · "
                f"{Path(request['repository_path']).name} · "
                f"{request['summary'][:80]}"
            )
        latest = requests[0]
        markup = (
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            button["text"],
                            callback_data=button["callback_data"],
                        )
                        for button in row
                    ]
                    for row in diagnose_markup(latest["id"])
                ]
            )
            if latest["status"] == "failed" and not latest["diagnosis_run_id"]
            else None
        )
        await update.message.reply_text("\n".join(lines), reply_markup=markup)

    async def _diagnose_deployment(
        self, *, user_id: int, chat_id: int, request_id: str, message
    ) -> None:
        try:
            request = self.database.get_deployment_request(request_id)
            session = self.database.active_session(user_id)
            run, created = self.database.enqueue_deployment_diagnosis(
                request_id=request_id,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session["id"],
                prompt=diagnosis_prompt(request),
            )
        except LookupError as error:
            await message.reply_text(str(error))
            return
        if not created:
            await message.reply_text(
                f"Deployment diagnosis already started as run {run['id'][:8]}."
            )
            return
        queue = self.database.queue_summary()
        await message.reply_text(
            f"🔧 Deployment diagnosis queued as run {run['id'][:8]} "
            f"(queue position {queue.get('queued', 0)})."
        )

    async def diagnose_deployment_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer("Preparing a new diagnosis run…")
        await self._diagnose_deployment(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            request_id=str(query.data).removeprefix("deploy:diagnose:"),
            message=query.message,
        )

    async def diagnose_deployment_menu_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        latest = self.database.latest_deployment_request(
            update.effective_user.id
        )
        if latest is None or latest["status"] != "failed":
            await update.message.reply_text(
                "No latest failed deployment needs diagnosis.",
                reply_markup=recovery_menu(),
            )
            return
        await self._diagnose_deployment(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            request_id=latest["id"],
            message=update.message,
        )

    async def investigate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        problem = update.message.text.partition(" ")[2].strip()
        if not problem:
            await update.message.reply_text(
                "Usage: /investigate <problem, error text, or Telegram link>"
            )
            return
        run, created = self.database.accept_message(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            text=problem,
            default_model=self.config.default_model,
            default_effort=self.config.default_effort,
            source="investigation",
            exact_prompt=investigation_prompt(problem),
        )
        queue = self.database.queue_summary()
        await update.message.reply_text(
            f"🔎 {'Investigation queued' if created else 'Already accepted'}"
            f" · run {run['id'][:8]} · position {queue.get('queued', 0)}"
        )

    async def status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.context_session(update)
        queue = self.database.queue_summary()
        lines = [
            "Receptionist2 status",
            f"Gateway: {self.database.get_state('gateway_status', 'unknown')}",
            f"Worker: {self.database.get_state('worker_status', 'unknown')}",
        ]
        if session:
            lines += [
                f"Session: {session['display_name']} ({session['id'][:8]})",
                f"Model: {session['model']}",
                f"Effort: {session['effort']}",
            ]
        lines += [
            f"Queued: {queue.get('queued', 0)}",
            f"Running: {queue.get('running', 0)}",
            f"Pending deliveries: {queue.get('outbox_pending', 0)}",
        ]
        if self.config.group_chat_id:
            topics = self.database.topic_sessions(update.effective_user.id)
            open_count = sum(1 for row in topics if row["status"] == "active")
            lines.append(f"Chat topics: {open_count} open · /chats")
        await update.message.reply_text("\n".join(lines))

    async def model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.context_session(update)
        if session is None:
            await self._topic_only(update, "/model")
            return
        if not context.args:
            await update.message.reply_text(
                f"Session model: {session['model']}\n"
                "Usage: /model opus|opus48|fable|sonnet|haiku|<full-id>\n"
                "Changes apply only to newly queued runs."
            )
            return
        try:
            model = resolve_model(context.args[0])
        except ValueError as error:
            await update.message.reply_text(str(error))
            return
        self.database.set_session_runtime(
            update.effective_user.id, model=model, session_id=session["id"]
        )
        await update.message.reply_text(
            f"Model set to {model} for newly queued runs."
        )

    async def new_session(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self.in_group(update):
            if not clean_name(" ".join(context.args)):
                await update.message.reply_text(
                    "Usage: /new <name>, e.g. /new Packers ATS"
                )
                return
            await self.name_new_chat(
                update.message, update.effective_user.id, " ".join(context.args)
            )
            return
        current = self.database.active_session(update.effective_user.id)
        name = " ".join(context.args).strip() or "conversation"
        session = self.database.create_session(
            update.effective_user.id,
            update.effective_chat.id,
            name=name,
            model=current["model"],
            effort=current["effort"],
        )
        await update.message.reply_text(
            f"New session {session['id'][:8]} · {session['display_name']}"
        )

    async def sessions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        active = self.database.active_session(update.effective_user.id)
        rows = self.database.list_sessions(update.effective_user.id)
        lines = ["Sessions:"]
        for row in rows[:20]:
            marker = "●" if row["id"] == active["id"] else "○"
            lines.append(
                f"{marker} {row['id'][:8]} · {row['display_name']} · "
                f"{row['model']} / {row['effort']}"
            )
        await update.message.reply_text("\n".join(lines))

    async def switch_session(
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

    async def effort(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.context_session(update)
        if session is None:
            await self._topic_only(update, "/effort")
            return
        level = context.args[0].lower() if context.args else ""
        if level not in EFFORT_LEVELS:
            await update.message.reply_text(
                f"Session effort: {session['effort']}\n"
                f"Usage: /effort {'|'.join(EFFORT_LEVELS)}"
            )
            return
        self.database.set_session_runtime(
            update.effective_user.id, effort=level, session_id=session["id"]
        )
        await update.message.reply_text(
            f"Effort set to {level} for newly queued runs."
        )

    async def restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(
            "How long should Receptionist2 wait for the active run?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Drain up to 10 min",
                            callback_data="r2:restart:10m",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Wait with no timeout",
                            callback_data="r2:restart:wait",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Interrupt now",
                            callback_data="r2:restart:now",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Cancel", callback_data="r2:restart:cancel"
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
        choice = str(query.data).rsplit(":", 1)[-1]
        if choice == "cancel":
            await query.edit_message_text("Restart cancelled.")
            return
        timeout = 0 if choice == "now" else None if choice == "wait" else 600
        request = self.database.create_restart_request(
            update.effective_user.id,
            update.effective_chat.id,
            timeout,
        )
        try:
            await query.message.set_reaction("⚡")
        except TelegramError:
            pass
        label = (
            "immediately"
            if timeout == 0
            else "after draining with no timeout"
            if timeout is None
            else "after draining for up to 10 minutes"
        )
        await query.edit_message_text(
            f"⚡ Restart {request['id'][:8]} queued {label}."
        )

    async def schedules(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        rows = self.database.list_schedules(update.effective_user.id)
        if not rows:
            await update.message.reply_text(
                "No recurring tasks.\n"
                "Add one with: /schedule_add name | cron | prompt"
            )
            return
        lines = ["Recurring tasks:"]
        for row in rows:
            marker = "▶" if row["enabled"] else "⏸"
            lines.append(
                f"{marker} {row['id'][:8]} · {row['name']} · "
                f"{row['cron']} {row['timezone']} · next {row['next_run_at']}"
            )
        await update.message.reply_text("\n".join(lines)[:4000])

    async def schedule_add(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        raw = update.message.text.partition(" ")[2]
        parts = [part.strip() for part in raw.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            await update.message.reply_text(
                "Usage: /schedule_add name | cron | prompt\n"
                "Example: /schedule_add morning | 0 8 * * * | Check updates"
            )
            return
        name, cron, prompt = parts
        try:
            scheduled = next_run(cron, str(self.config.timezone))
            session = self.database.active_session(update.effective_user.id)
            row = self.database.create_schedule(
                user_id=update.effective_user.id,
                name=name,
                cron=cron,
                timezone=str(self.config.timezone),
                prompt=prompt,
                model=session["model"],
                effort=session["effort"],
                next_run_at=scheduled.isoformat(),
            )
        except (ValueError, RuntimeError) as error:
            await update.message.reply_text(f"Could not create schedule: {error}")
            return
        await update.message.reply_text(
            f"Created {row['name']} ({row['id'][:8]}), next run "
            f"{row['next_run_at']}."
        )

    async def schedule_toggle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        enabled: bool,
    ) -> None:
        if not context.args:
            await update.message.reply_text(
                f"Usage: /schedule_{'resume' if enabled else 'pause'} <id-prefix>"
            )
            return
        try:
            row = self.database.set_schedule_enabled(
                update.effective_user.id, context.args[0], enabled
            )
        except LookupError as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"{'Resumed' if enabled else 'Paused'} {row['name']}."
        )

    async def schedule_pause(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.schedule_toggle(update, context, enabled=False)

    async def schedule_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.schedule_toggle(update, context, enabled=True)

    async def schedule_run(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: /schedule_run <id-prefix>"
            )
            return
        try:
            schedule = self.database.schedule_by_prefix(
                update.effective_user.id, context.args[0]
            )
            scheduled_for = datetime.now(UTC).isoformat()
            run, created = self.database.enqueue_scheduled_run(
                schedule, scheduled_for
            )
        except (LookupError, RuntimeError) as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"{'Queued' if created else 'Already queued'} {schedule['name']} "
            f"as run {run['id'][:8]}."
        )

    async def exact_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        existing = self.database.inbound_run(
            update.effective_chat.id, message.message_id
        )
        if self.in_group(update):
            await self._accept(
                message,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                message_ids=[message.message_id],
                text=message.text,
            )
            return
        if existing is None:
            session = self.database.active_session(update.effective_user.id)
            reason = self.database.session_rollover_reason(
                session["id"], message.text
            )
            if reason:
                pending = self.database.create_pending_rollover(
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    message_id=message.message_id,
                    session_id=session["id"],
                    text=message.text,
                    reason=reason,
                )
                await message.reply_text(
                    "🧭 A fresh session may be more efficient.\n"
                    f"{reason}\n\nStart a new session for this message?",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Start fresh",
                                    callback_data=f"session:new:{pending['id']}",
                                ),
                                InlineKeyboardButton(
                                    "Continue current",
                                    callback_data=(
                                        f"session:continue:{pending['id']}"
                                    ),
                                ),
                            ]
                        ]
                    ),
                )
                return
        await self._accept_text(
            message,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            message_id=message.message_id,
            text=message.text,
        )

    async def image_receive(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        existing = self.database.inbound_run(
            update.effective_chat.id, message.message_id
        )
        if existing:
            await message.reply_text(
                f"Already accepted · run {existing['id'][:8]}"
            )
            return
        if self.in_group(update) and self.is_hub(thread_of(message)):
            group = message.media_group_id
            if group is None or group not in self._refused_albums:
                if group is not None:
                    self._refused_albums.add(group)
                await message.reply_text(NAME_FIRST_TEXT)
            return
        image = telegram_image(message)
        if image is None:
            await message.reply_text(
                "Send screenshots as photos or as PNG, JPEG, WebP, or GIF "
                "files."
            )
            return
        if image.size_bytes is not None and image.size_bytes > IMAGE_MAX_BYTES:
            await message.reply_text("That image is larger than 20 MB.")
            return
        try:
            path = await self._store_image(image)
        except (OSError, TelegramError, ValueError) as error:
            log.exception("Could not store image message")
            await message.reply_text(f"Could not save that image: {error}")
            return
        caption = message.caption or ""
        group = message.media_group_id
        if group is None:
            await self._accept(
                message,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                message_ids=[message.message_id],
                text=image_summary(caption, 1),
                exact_prompt=image_prompt(caption, [str(path)]),
            )
            return
        album = self._albums.setdefault(
            group,
            {
                "user_id": update.effective_user.id,
                "chat_id": update.effective_chat.id,
                "messages": [],
                "caption": "",
            },
        )
        album["messages"].append((message.message_id, message, str(path)))
        if caption:
            album["caption"] = caption
        name = f"album:{group}"
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        context.job_queue.run_once(
            self._accept_album, ALBUM_SETTLE_SECONDS, data=group, name=name
        )

    async def _accept_album(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        album = self._albums.pop(context.job.data, None)
        if not album:
            return
        items = sorted(album["messages"], key=lambda item: item[0])
        first_id, first_message, _ = items[0]
        paths = [path for _, _, path in items]
        await self._accept(
            first_message,
            user_id=album["user_id"],
            chat_id=album["chat_id"],
            message_ids=[message_id for message_id, _, _ in items],
            text=image_summary(album["caption"], len(paths)),
            exact_prompt=image_prompt(album["caption"], paths),
        )

    async def _store_image(self, image: TelegramImage) -> Path:
        directory = self.config.images_dir
        directory.mkdir(parents=True, exist_ok=True, mode=SHARED_DIR_MODE)
        directory.chmod(SHARED_DIR_MODE)
        final_path = directory / f"{uuid.uuid4()}{image.suffix}"
        temp_path = directory / f".{final_path.name}.part"
        try:
            telegram_file = await image.media.get_file()
            await telegram_file.download_to_drive(custom_path=temp_path)
            if temp_path.stat().st_size > IMAGE_MAX_BYTES:
                raise ValueError("the image exceeded the 20 MB limit")
            temp_path.chmod(0o660)
            temp_path.replace(final_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return final_path

    async def _topic_session_for(self, message, user_id: int) -> dict:
        """The session of the topic a message is in, adopting or reopening it."""
        chat_id = self.config.group_chat_id
        thread = thread_of(message)
        session = self.database.topic_session(chat_id, thread)
        if session is None:
            created = getattr(message.reply_to_message, "forum_topic_created", None)
            return await self.adopt_topic(
                message.get_bot(),
                user_id,
                thread,
                created.name if created else f"Topic {thread}",
            )
        if session["status"] != "active":
            try:
                await message.get_bot().reopen_forum_topic(chat_id, thread)
            except TelegramError as error:
                log.warning("Could not reopen topic %s: %s", thread, error)
            session = self.database.set_topic_open(chat_id, thread, True)
        return session

    async def audio_receive(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Voice notes and audio files: transcribe, then offer Run or Trash."""
        message = update.effective_message
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        in_group = self.in_group(update)
        if not in_group and self.database.active_nfl_audio_collection(
            user_id, chat_id
        ):
            await self.nfl_audio_receive(update, context)
            return
        selected = telegram_audio(message)
        if selected is None:
            await message.reply_text(
                "That file isn't audio or an image. Send a voice note, an "
                "audio file, or a screenshot."
            )
            return
        if in_group and self.is_hub(thread_of(message)):
            await message.reply_text(NAME_FIRST_TEXT)
            return
        if (
            selected.duration_seconds is not None
            and selected.duration_seconds
            > self.config.nfl_audio_max_duration_seconds
        ):
            await message.reply_text("That audio is longer than an hour.")
            return
        size = getattr(selected.media, "file_size", None)
        if size is not None and size > self.config.nfl_audio_max_bytes:
            await message.reply_text("That audio is larger than 20 MB.")
            return
        session = (
            await self._topic_session_for(message, user_id)
            if in_group
            else self.database.active_session(user_id)
        )
        note, created = self.database.create_voice_note(
            session_id=session["id"],
            user_id=user_id,
            chat_id=chat_id,
            message_id=message.message_id,
        )
        # A redelivered update is dropped unless its first attempt died
        # before the audio was saved (a gateway crash mid-download).
        unfinished = (
            note["status"] == "transcribing"
            and not note["local_path"]
            and note["id"] not in self._downloading
        )
        if not created and not unfinished:
            return
        self._downloading.add(note["id"])
        try:
            await self._download_note(message, note, selected)
        finally:
            self._downloading.discard(note["id"])

    async def _download_note(self, message, note: dict, selected) -> None:
        bot = message.get_bot()
        status_id = note.get("status_message_id")
        if not status_id:
            status = await message.reply_text("🎧 Transcribing…")
            status_id = status.message_id
            self.database.update_voice_note(note["id"], status_message_id=status_id)
        try:
            path = await self._store_voice(selected)
        except (OSError, TelegramError, ValueError) as error:
            log.exception("Could not store voice note")
            self.database.update_voice_note(
                note["id"], status="failed", error=str(error)
            )
            await self._show_voice_status(
                bot, note, status_id, f"❌ Could not save that audio: {error}"
            )
            return
        self.database.update_voice_note(note["id"], local_path=str(path))
        self._start_transcription(bot, self.database.voice_note(note["id"]))

    async def _show_voice_status(
        self, bot, note: dict, status_id, text: str, markup=None
    ) -> None:
        if not status_id:
            return
        try:
            await bot.edit_message_text(
                text,
                chat_id=int(note["telegram_chat_id"]),
                message_id=int(status_id),
                reply_markup=markup,
            )
        except TelegramError as error:
            log.warning("Could not update the voice note message: %s", error)

    async def _store_voice(self, selected) -> Path:
        directory = self.config.voice_dir
        directory.mkdir(parents=True, exist_ok=True, mode=SHARED_DIR_MODE)
        directory.chmod(SHARED_DIR_MODE)
        final_path = directory / f"{uuid.uuid4()}{selected.suffix}"
        temp_path = directory / f".{final_path.name}.part"
        try:
            telegram_file = await selected.media.get_file()
            await telegram_file.download_to_drive(custom_path=temp_path)
            if temp_path.stat().st_size > self.config.nfl_audio_max_bytes:
                raise ValueError("the audio exceeded the 20 MB limit")
            temp_path.chmod(0o660)
            temp_path.replace(final_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return final_path

    def _start_transcription(self, bot, note: dict) -> None:
        task = asyncio.create_task(self._transcribe_note(bot, note))
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_tasks.discard)

    async def _run_whisper(self, path: str) -> dict:
        """Transcribe in a subprocess so the model is freed afterwards."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "receptionist2.voice",
            path,
            self.config.whisper_model,
            str(self.config.whisper_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The unit's home is read-only; keep Hugging Face's cache writable.
            env={**os.environ, "HF_HOME": str(self.config.whisper_dir / "hf-home")},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), TRANSCRIBE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"error": "Transcription took longer than 15 minutes."}
        try:
            return json.loads(stdout.decode().strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {
                "error": (stderr.decode(errors="replace").strip()[-500:])
                or f"Transcriber exited with {process.returncode}"
            }

    async def _transcribe_note(self, bot, note: dict) -> None:
        if not note.get("local_path") or not Path(note["local_path"]).exists():
            self.database.update_voice_note(
                note["id"], status="failed", error="The audio file is missing."
            )
            await self._show_voice_status(
                bot,
                note,
                note.get("status_message_id"),
                "❌ The audio was lost during a restart; send it again.",
            )
            return
        async with self._transcribe_lock:
            result = await self._run_whisper(note["local_path"])
        transcript = str(result.get("transcript") or "").strip()
        if transcript:
            self.database.update_voice_note(
                note["id"], status="ready", transcript=transcript
            )
            text = f"🎧 Heard:\n{transcript_preview(transcript)}"
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "▶️ Run", callback_data=f"r2:voice:run:{note['id']}"
                        ),
                        InlineKeyboardButton(
                            "🗑 Trash", callback_data=f"r2:voice:trash:{note['id']}"
                        ),
                    ]
                ]
            )
        else:
            error = str(result.get("error") or "No transcript was produced.")
            self.database.update_voice_note(note["id"], status="failed", error=error)
            Path(note["local_path"]).unlink(missing_ok=True)
            text, markup = f"❌ Could not transcribe that audio: {error}", None
        await self._show_voice_status(
            bot, note, note.get("status_message_id"), text, markup
        )

    async def voice_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        _, _, action, note_id = str(query.data).split(":", 3)
        note = self.database.claim_voice_note(
            note_id, "queued" if action == "run" else "trashed"
        )
        if note is None:
            await query.answer("That voice note was already handled.")
            return
        await query.answer()
        if action == "trash":
            if note.get("local_path"):
                Path(note["local_path"]).unlink(missing_ok=True)
            try:
                await query.edit_message_text("🗑 Trashed. The audio was deleted.")
            except TelegramError:
                pass
            return
        try:
            await query.edit_message_text(
                f"🎧 Heard:\n{transcript_preview(note['transcript'])}"
            )
        except TelegramError:
            pass
        try:
            run = await self._accept_text(
                query.message,
                user_id=update.effective_user.id,
                chat_id=int(note["telegram_chat_id"]),
                message_id=int(note["telegram_message_id"]),
                text=note["transcript"],
                exact_prompt=voice_prompt(note["transcript"]),
                session_id=note["session_id"],
            )
        except Exception:
            log.exception("Could not queue voice note %s", note_id)
            self.database.update_voice_note(note_id, status="ready")
            await query.message.reply_text(
                "❌ Could not queue that voice note. Send /recover, then try "
                "Run again."
            )
            raise
        self.database.set_voice_note_run(note_id, run["id"])

    async def tombstone(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Have this chat write a handoff file for a fresh chat."""
        session = self.context_session(update)
        if session is None:
            await self._topic_only(update, "/tombstone")
            return
        path = tombstone_path(
            self.config.tombstone_dir, session["id"], datetime.now(UTC)
        )
        run = self.database.enqueue_run(
            session_id=session["id"],
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            source="message",
            prompt=write_prompt(path, session["display_name"]),
        )
        self.database.update_session(
            session["id"], tombstone_path=str(path), tombstone_run_id=run["id"]
        )
        await update.message.reply_text(
            f"🪦 Writing the tombstone for {session['display_name']} · run "
            f"{run['id'][:8]}. When it's saved you can continue in a new chat."
        )

    async def knowledge_transfer(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Have this chat record its reusable learnings in the knowledge base."""
        session = self.context_session(update)
        if session is None:
            await self._topic_only(update, "/knowledge_transfer")
            return
        focus = re.sub(r"^/\S+", "", update.message.text, count=1).strip()
        run = self.database.enqueue_run(
            session_id=session["id"],
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            source="message",
            prompt=knowledge_transfer_prompt(focus),
        )
        await update.message.reply_text(
            f"📚 Recording what {session['display_name']} learned in "
            f"www/knowledge_base · run {run['id'][:8]}."
        )

    async def tomb_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        _, _, action, session_id = str(query.data).split(":", 3)
        try:
            old = self.database.get_session(session_id)
        except LookupError:
            await query.answer("That chat no longer exists.")
            return
        if action == "new":
            await self._continue_from_tombstone(update, context, old)
        elif action == "del":
            await query.answer()
            await query.edit_message_text(
                f"Delete {old['display_name']}? "
                + (
                    "This removes all of its messages permanently."
                    if old["telegram_thread_id"]
                    else "The session is archived and can't be switched to."
                )
                + f" The tombstone stays at {old['tombstone_path']}.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🗑 Yes, delete",
                                callback_data=f"r2:tomb:delyes:{session_id}",
                            ),
                            InlineKeyboardButton(
                                "Keep", callback_data=f"r2:tomb:keep:{session_id}"
                            ),
                        ]
                    ]
                ),
            )
        elif action == "keep":
            await query.answer("Kept.")
            await query.edit_message_text(
                self._continued_text(old), reply_markup=self._delete_markup(old)
            )
        else:
            await self._delete_chat(update, context, old)

    def _continued_text(self, old: dict) -> str:
        successor = self.database.get_session(old["successor_session_id"])
        where = (
            topic_link(
                int(successor["telegram_chat_id"]),
                int(successor["telegram_thread_id"]),
            )
            if successor["telegram_thread_id"]
            else f"session {successor['id'][:8]}, now the active one here"
        )
        return (
            f"🪦 Tombstone saved:\n{old['tombstone_path']}\n\n"
            f"➕ Continued in {successor['display_name']}: {where}"
        )

    @staticmethod
    def _delete_markup(old: dict) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Delete this chat",
                        callback_data=f"r2:tomb:del:{old['id']}",
                    )
                ]
            ]
        )

    async def _continue_from_tombstone(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, old: dict
    ) -> None:
        query = update.callback_query
        if old.get("successor_session_id"):
            await query.answer("Already continued.")
            await query.edit_message_text(
                self._continued_text(old), reply_markup=self._delete_markup(old)
            )
            return
        path = old.get("tombstone_path")
        if not path or not Path(path).is_file():
            await query.answer("The tombstone file is missing.", show_alert=True)
            return
        user_id = update.effective_user.id
        name = continuation_name(
            old["display_name"].split(" ", 1)[1]
            if old["telegram_thread_id"] and has_fruit(old["display_name"])
            else old["display_name"]
        )
        if old["telegram_thread_id"]:
            try:
                new = await self.open_topic(
                    context.bot,
                    user_id=user_id,
                    name=name,
                    greeting=(
                        f"🪦 Continuing from {old['display_name']}'s tombstone. "
                        "Reading it now…"
                    ),
                )
            except TelegramError as error:
                await query.answer(f"Could not open a topic: {error}", show_alert=True)
                return
            chat_id = self.config.group_chat_id
        else:
            chat_id = update.effective_chat.id
            new = self.database.create_session(
                user_id, chat_id, name=name, model=old["model"], effort=old["effort"]
            )
        self.database.set_session_runtime(
            user_id, model=old["model"], effort=old["effort"], session_id=new["id"]
        )
        if old.get("instructions"):
            self.database.set_session_instructions(new["id"], old["instructions"])
        self.database.update_session(old["id"], successor_session_id=new["id"])
        self.database.update_session(new["id"], predecessor_session_id=old["id"])
        self.database.enqueue_run(
            session_id=new["id"],
            user_id=user_id,
            chat_id=chat_id,
            source="message",
            prompt=resume_prompt(Path(path)),
        )
        await query.answer(f"Continuing in {new['display_name']}")
        await query.edit_message_text(
            self._continued_text(self.database.get_session(old["id"])),
            reply_markup=self._delete_markup(old),
        )

    async def _delete_chat(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, old: dict
    ) -> None:
        query = update.callback_query
        if self.database.session_running(old["id"]):
            await query.answer(
                "This chat still has a queued or running run. Stop it first.",
                show_alert=True,
            )
            return
        if not old["telegram_thread_id"]:
            if old["status"] == "active":
                user = self.database.get_user_state(update.effective_user.id)
                if user["active_session_id"] == old["id"]:
                    await query.answer(
                        "That is still the active session.", show_alert=True
                    )
                    return
            self.database.update_session(old["id"], status="archived")
            await query.answer("Archived.")
            await query.edit_message_text(
                f"🗑 Archived {old['display_name']}. The tombstone stays at "
                f"{old['tombstone_path']}."
            )
            return
        chat_id = int(old["telegram_chat_id"])
        try:
            await context.bot.delete_forum_topic(
                chat_id, int(old["telegram_thread_id"])
            )
        except TelegramError as error:
            await query.answer(f"Could not delete the topic: {error}", show_alert=True)
            return
        self.database.set_topic_open(chat_id, int(old["telegram_thread_id"]), False)
        await query.answer("Deleted.")
        successor_id = old.get("successor_session_id")
        if successor_id:
            successor = self.database.get_session(successor_id)
            if successor["telegram_thread_id"]:
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"🗑 Deleted {old['display_name']}. Its tombstone: "
                        f"{old['tombstone_path']}",
                        message_thread_id=int(successor["telegram_thread_id"]),
                    )
                except TelegramError as error:
                    log.warning("Could not confirm the deletion: %s", error)

    async def instructions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        session = self.context_session(update)
        if session is None:
            await self._topic_only(update, "/instructions")
            return
        # Drop only the command token so multi-line instructions survive.
        raw = re.sub(r"^/\S+", "", update.message.text, count=1).strip()
        if not raw:
            current = session.get("instructions")
            await update.message.reply_text(
                f"📌 Instructions for {session['display_name']}:\n{current}"
                if current
                else (
                    "This chat has no standing instructions. Set them with "
                    "/instructions <text or skill path>; every run here will "
                    "follow them. /instructions clear removes them."
                )
            )
            return
        if raw.lower() in {"clear", "off", "none"}:
            self.database.set_session_instructions(session["id"], None)
            await update.message.reply_text("📌 Instructions cleared.")
            return
        if len(raw) > INSTRUCTIONS_MAX_CHARS:
            await update.message.reply_text(
                "Those instructions are too long for one message. Put them in "
                "a skill file in the repo and set /instructions <its path>."
            )
            return
        self.database.set_session_instructions(session["id"], raw)
        await update.message.reply_text(
            f"📌 Every run in {session['display_name']} will now follow:\n{raw}"
        )

    async def _accept(
        self,
        message,
        *,
        user_id: int,
        chat_id: int,
        message_ids: list[int],
        text: str,
        exact_prompt: str | None = None,
    ) -> None:
        """Queue a message: in the group, into its topic's own session."""
        message_id = message_ids[0]
        if chat_id != self.config.group_chat_id:
            await self._accept_text(
                message,
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                exact_prompt=exact_prompt,
            )
            return
        existing = self.database.inbound_run(chat_id, message_id)
        if existing:
            await message.reply_text(
                f"Already accepted · run {existing['id'][:8]}"
            )
            return
        thread = thread_of(message)
        if self.is_hub(thread):
            # Writing in the hub (or General) names a new chat; nothing runs.
            if getattr(message, "edit_date", None):
                return  # editing an earlier name must not open another chat
            if exact_prompt is None:
                await self.name_new_chat(message, user_id, text)
            else:
                await message.reply_text(NAME_FIRST_TEXT)
            return
        session = await self._topic_session_for(message, user_id)
        await self._accept_text(
            message,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            exact_prompt=exact_prompt,
            session_id=session["id"],
        )

    async def _accept_text(
        self,
        reply_to,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        exact_prompt: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        run, created = self.database.accept_message(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            default_model=self.config.default_model,
            default_effort=self.config.default_effort,
            exact_prompt=exact_prompt,
            session_id=session_id,
        )
        if created:
            try:
                await reply_to.get_bot().set_message_reaction(
                    chat_id, message_id, "👀"
                )
            except TelegramError:
                pass
        queue = self.database.queue_summary()
        ack = (
            f"{'Queued' if created else 'Already accepted'} · run "
            f"{run['id'][:8]}\n"
            f"{run['model']} · effort {run['effort']} · "
            f"position {queue.get('queued', 0)}"
        )
        await reply_to.reply_text(ack)
        return run

    async def session_rollover_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        action, pending_id = str(query.data).split(":")[1:]
        user_id = update.effective_user.id
        try:
            pending = self.database.get_pending_rollover(user_id, pending_id)
        except LookupError as error:
            await query.answer(str(error), show_alert=True)
            return
        active = self.database.active_session(user_id)
        if action == "continue" and active["id"] != pending["session_id"]:
            await query.answer(
                "The active session changed; resend the message.",
                show_alert=True,
            )
            return
        try:
            pending = self.database.consume_pending_rollover(
                user_id, pending_id
            )
        except LookupError as error:
            await query.answer(str(error), show_alert=True)
            return
        if action == "new":
            session = self.database.create_session(
                user_id,
                int(pending["telegram_chat_id"]),
                name="conversation",
                model=active["model"],
                effort=active["effort"],
            )
            decision = f"Started fresh session {session['id'][:8]}."
        else:
            decision = f"Continuing session {active['id'][:8]}."
        await query.answer(decision)
        try:
            await query.edit_message_text(f"🧭 {decision}")
        except TelegramError:
            pass
        await self._accept_text(
            query.message,
            user_id=user_id,
            chat_id=int(pending["telegram_chat_id"]),
            message_id=int(pending["telegram_message_id"]),
            text=pending["exact_text"],
        )

    async def update_progress(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(UTC)
        running = self.database.running_runs()
        active_ids = {run["id"] for run in running}
        for run_id in list(self._progress_edits):
            if run_id not in active_ids:
                self._progress_edits.pop(run_id, None)
        for run in running:
            chat_id = int(run["telegram_chat_id"])
            thread = run.get("telegram_thread_id")
            if thread:
                try:
                    verbose = bool(
                        self.database.get_user_state(
                            int(run["telegram_user_id"])
                        )["verbose"]
                    )
                except LookupError:
                    verbose = False
            else:
                verbose = self.database.verbose_for_chat(chat_id)
            try:
                if not run["status_message_id"]:
                    sent = await context.bot.send_message(
                        chat_id,
                        f"▶️ Run {run['id'][:8]} started · {run['model']} / "
                        f"{run['effort']}\nClaude is starting…",
                        message_thread_id=thread,
                    )
                    self.database.set_status_message(run["id"], sent.message_id)
                    self._progress_edits[run["id"]] = time.monotonic()
                    continue
                interval = 30 if verbose else 45
                last = self._progress_edits.get(run["id"], 0.0)
                if time.monotonic() - last < interval:
                    continue
                self._progress_edits[run["id"]] = time.monotonic()
                await context.bot.edit_message_text(
                    progress_text(run, verbose=verbose, now=now),
                    chat_id=chat_id,
                    message_id=int(run["status_message_id"]),
                )
            except BadRequest as error:
                if "not modified" not in str(error).lower():
                    log.warning("Progress update failed: %s", error)
            except TelegramError as error:
                log.warning("Progress update failed: %s", error)

    async def deliver_outbox(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for _ in range(10):
            item = self.database.claim_outbox()
            if item is None:
                return
            thread = item.get("telegram_thread_id")
            try:
                if item["kind"] == "run_result" and item["run_id"]:
                    await self._finish_status_message(context, item["run_id"])
                try:
                    await self._send_chunks(
                        context, item["telegram_chat_id"], item, thread
                    )
                except BadRequest as error:
                    if not thread or "thread" not in str(error).lower():
                        raise
                    # The topic was deleted: deliver to the private chat
                    # rather than blocking every later delivery.
                    log.warning("Topic %s is gone; delivering privately", thread)
                    await self._send_chunks(
                        context,
                        self.config.allowed_user_id,
                        item,
                        None,
                        prefix="(from a deleted chat topic)\n",
                    )
            except TelegramError as error:
                self.database.finish_outbox(item["id"], str(error))
                return
            self.database.finish_outbox(item["id"])

    async def _send_chunks(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        item: dict,
        thread: int | None,
        *,
        prefix: str = "",
    ) -> None:
        body = prefix + item["body"]
        chunks = [
            body[start : start + 3900] for start in range(0, len(body), 3900)
        ] or [""]
        markup = markup_from_json(item.get("reply_markup_json"))
        for index, chunk in enumerate(chunks):
            await context.bot.send_message(
                chat_id,
                chunk,
                message_thread_id=thread,
                reply_markup=markup if index == len(chunks) - 1 else None,
            )

    async def _finish_status_message(
        self, context: ContextTypes.DEFAULT_TYPE, run_id: str
    ) -> None:
        run = self.database.get_run(run_id)
        if not run["status_message_id"]:
            return
        try:
            await context.bot.edit_message_text(
                f"{RUN_ICONS.get(run['status'], '•')} Run {run_id[:8]} "
                f"{run['status']}.",
                chat_id=int(run["telegram_chat_id"]),
                message_id=int(run["status_message_id"]),
            )
        except TelegramError:
            pass
        self._progress_edits.pop(run_id, None)

    async def enqueue_due_schedules(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        now = datetime.now(UTC)
        for schedule in self.database.due_schedules(now.isoformat()):
            scheduled_for = schedule["next_run_at"]
            self.database.enqueue_scheduled_run(schedule, scheduled_for)
            following = next_run(
                schedule["cron"], schedule["timezone"], after=now
            )
            self.database.advance_schedule(
                schedule["id"],
                last_run_at=scheduled_for,
                next_run_at=following.isoformat(),
            )


def build_application(config: GatewayConfig) -> Application:
    gateway = Gateway(config, Database(config.database_path))
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(gateway.post_init)
        .post_shutdown(gateway.post_shutdown)
        .build()
    )
    commands = [
        ("start", gateway.start),
        ("help", gateway.help),
        ("nfl_audio", gateway.nfl_audio_start),
        ("nfl_audio_done", gateway.nfl_audio_done),
        ("nfl_audio_cancel", gateway.nfl_audio_cancel),
        ("status", gateway.status),
        ("topics", gateway.topics),
        ("verbose", gateway.verbose),
        ("stop", gateway.stop),
        ("recover", gateway.recover),
        ("reset", gateway.reset),
        ("deployments", gateway.deployments),
        ("investigate", gateway.investigate),
        ("new", gateway.new_session),
        ("sessions", gateway.sessions),
        ("switch", gateway.switch_session),
        ("model", gateway.model),
        ("effort", gateway.effort),
        ("restart", gateway.restart),
        ("schedules", gateway.schedules),
        ("schedule_add", gateway.schedule_add),
        ("schedule_pause", gateway.schedule_pause),
        ("schedule_resume", gateway.schedule_resume),
        ("schedule_run", gateway.schedule_run),
    ]
    commands.append(("chats", gateway.chats))
    commands.append(("instructions", gateway.instructions))
    commands.append(("tombstone", gateway.tombstone))
    commands.append(("knowledge_transfer", gateway.knowledge_transfer))
    group_commands = {
        "help", "new", "stop", "status", "model", "effort", "chats",
        "instructions", "tombstone", "knowledge_transfer",
    }
    for name, handler in commands:
        application.add_handler(
            CommandHandler(
                name,
                gateway.authorized(handler, group=name in group_commands),
            )
        )
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED
            | filters.StatusUpdate.FORUM_TOPIC_EDITED
            | filters.StatusUpdate.FORUM_TOPIC_CLOSED
            | filters.StatusUpdate.FORUM_TOPIC_REOPENED,
            gateway.authorized(gateway.topic_event, group=True),
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            gateway.authorized(gateway.new_chat_button, group=True),
            pattern=f"^{NEW_CHAT_CALLBACK}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            gateway.authorized(gateway.voice_button, group=True),
            pattern=VOICE_PATTERN,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            gateway.authorized(gateway.tomb_button, group=True),
            pattern=TOMB_PATTERN,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            gateway.authorized(gateway.restart_button),
            pattern=RESTART_PATTERN,
        )
    )
    for pattern, handler in (
        (ROLLOVER_PATTERN, gateway.session_rollover_button),
        (DIAGNOSE_PATTERN, gateway.diagnose_deployment_button),
        (RETRY_PATTERN, gateway.retry_button),
    ):
        application.add_handler(
            CallbackQueryHandler(gateway.authorized(handler), pattern=pattern)
        )
    for text, handler in (
        (RECOVER_MENU_TEXT, gateway.recover),
        (DIAGNOSE_DEPLOYMENT_TEXT, gateway.diagnose_deployment_menu_button),
        (TOPICS_MENU_TEXT, gateway.topics),
        (STOP_MENU_TEXT, gateway.stop),
        (STATUS_MENU_TEXT, gateway.status),
    ):
        application.add_handler(
            MessageHandler(
                filters.Regex(f"^{re.escape(text)}$"),
                gateway.authorized(handler),
            )
        )
    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            gateway.authorized(gateway.image_receive, group=True),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.Document.ALL,
            gateway.authorized(gateway.audio_receive, group=True),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            gateway.authorized(gateway.exact_text, group=True),
        )
    )
    return application


def main() -> None:
    config = GatewayConfig.from_env()
    # Telegram keeps the last allowed_updates a bot polled with, so request
    # every type explicitly rather than inheriting a narrower setting.
    build_application(config).run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
