from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude import ClaudeClient
from .config import Config
from .espn import EspnClient, match_final
from .lines import SheetLines
from .markets import match_event
from .models import FinalGame, MarketEvent, parse_timestamp, team_matches, utc_now
from .store import DecisionStore
from .transcribe import WhisperTranscriber


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("decision_helper")
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
EASTERN = ZoneInfo("America/New_York")
CANDIDATE_WINDOW = timedelta(days=8)
STARTED_LOOKBACK_DAYS = 3
HISTORIC_LINES_AFTER = timedelta(minutes=10)
LEARNING_HOUR = 9  # Eastern; a day's learnings run the next morning
LEARNING_RETRY = timedelta(minutes=30)
LEARNING_REVIEW_ATTEMPTS = 3  # a late day still waits on reviews until they fail this often
TELEGRAM_LIMIT = 4096
SUMMARY_DAY_ROLLOVER = timedelta(hours=6)  # a 1 AM summary is about yesterday
AUDIO_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
HELP_TEXT = (
    "I'm Mel. Send a voice note (or text) with your NFL/NBA thoughts — "
    "winners, spreads, and totals. For each voice note I ask whether to "
    "parse it or trash it. I just confirm what I recorded. Send as many notes "
    "per game as you like: your latest side is your decision, earlier ones "
    "form its path, and thoughts without a side are attached to the game. "
    "I track BetOnline lines and grade and review every game quietly; see "
    "/stats. Each morning I study the previous day's thoughts and results "
    "for learnings (/learnings) and use them in my opinions. Each saved note has Reparse and Remove buttons, and I keep "
    "your audio and its transcript so notes can be reparsed later.\n\n"
    "🧾 End of day: send a summary of the bets you actually placed and why, "
    "and tap \"Parse as end of day\" (or start a text with \"End of day "
    "summary\"). Those become your bets, compared against your leans; I ask "
    "about any recorded bet the summary leaves out.\n\n"
    "/games - today's and tomorrow's games; tap one for my opinion\n"
    "/recent - latest 10 picks and leans\n"
    "/stats - record, calibration, closing-line value, process, biases\n"
    "/learnings [YYYY-MM-DD] - what a day of decisions taught (latest by "
    "default; I write one each morning at 9 ET)\n"
    "/reparse - re-run parsing on your latest note, or reply /reparse to "
    "any of your notes\n"
    "/void <id-prefix> - remove a mis-parsed open pick"
)


def choice_keyboard(entry_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧠 Parse", callback_data=f"parse:{entry_id}"),
                InlineKeyboardButton("🗑 Trash", callback_data=f"trash:{entry_id}"),
            ],
            [
                InlineKeyboardButton(
                    "🧾 Parse as end of day", callback_data=f"summary:{entry_id}"
                ),
            ],
        ]
    )


def omitted_keyboard(pick_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Still a bet", callback_data=f"keepbet:{pick_id}"),
                InlineKeyboardButton("↩️ It was a lean", callback_data=f"waslean:{pick_id}"),
            ]
        ]
    )


SUMMARY_TEXT = re.compile(r"^\W*end[\s-]*of[\s-]*(the[\s-]*)?day\b", re.IGNORECASE)


def remove_keyboard(entry_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Reparse", callback_data=f"reparse:{entry_id}"),
                InlineKeyboardButton("🗑 Remove this note", callback_data=f"remove:{entry_id}"),
            ]
        ]
    )


def describe_pick(pick: dict[str, Any]) -> str:
    matchup = f"{pick['away_team']} @ {pick['home_team']}"
    if pick["pick_type"] == "winner":
        detail = f"{pick['selection']} to win"
    elif pick["pick_type"] == "spread":
        line = pick["line"]
        detail = (
            f"{pick['selection']} {line:+g}"
            if line is not None
            else f"{pick['selection']} spread (no line)"
        )
    else:
        line = pick["line"]
        detail = (
            f"{pick['selection']} {line:g}"
            if line is not None
            else f"{pick['selection']} (no total)"
        )
    if pick["line_source"] == "market":
        detail += (
            " [BetOnline close]"
            if pick.get("logged_after_start")
            else " [BetOnline line]"
        )
    if pick.get("logged_after_start"):
        detail += " [logged after start]"
    if pick["price_at_pick"] is not None:
        detail += f" ({pick['price_at_pick']:+d})"
    spread = pick.get("spread_at_pick")
    if spread is not None and not (
        pick["pick_type"] == "spread" and pick["line"] == spread
    ):
        number = "pk" if spread == 0 else f"{short_team(pick['spread_team'])} {spread:+g}"
        detail += f" [BetOnline spread {number}]"
    low, high = pick.get("my_line_low"), pick.get("my_line_high")
    if low is not None:
        fmt = "{:g}" if pick["pick_type"] == "total" else "{:+g}"
        mine = fmt.format(low)
        if high is not None and high != low:
            mine += " to " + fmt.format(high)
        detail += f" [your number {mine}]"
    confidence = ""
    if pick["confidence_probability"] is not None:
        confidence = f" · {pick['confidence_probability']:.0%}"
    elif pick["confidence_label"]:
        confidence = f" · {pick['confidence_label']}"
    label = "Lean · " if pick.get("commitment") == "lean" else "Bet · "
    if pick.get("role") == "superseded":
        label = "↩ Earlier view · " + label
    return (
        f"{pick['id'][:8]} · {label}{pick['sport'].upper()} · {matchup}: "
        f"{detail}{confidence}"
    )


def format_saved(
    picks: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    transcript: str,
    passes: list[dict[str, Any]] | None = None,
) -> str:
    passes = passes or []
    excerpt = " ".join(transcript.split())
    if len(excerpt) > 600:
        excerpt = excerpt[:597] + "..."
    lines = [f"📝 Heard: “{excerpt}”", ""]
    if picks:
        finals = [pick for pick in picks if pick.get("role") != "superseded"]
        committed = sum(pick.get("commitment") != "lean" for pick in finals)
        counts = []
        if committed:
            counts.append(f"{committed} bet(s)")
        if len(finals) - committed:
            counts.append(f"{len(finals) - committed} lean(s)")
        earlier = len(picks) - len(finals)
        if earlier:
            counts.append(f"{earlier} earlier view(s) you moved off")
        lines.append(f"✅ Saved {', '.join(counts)}:")
        lines.extend(f"• {describe_pick(pick)}" for pick in picks)
    elif not notes and not passes:
        lines.append(
            "No NFL/NBA games, sides, or totals were found in that note."
        )
    if passes:
        if picks:
            lines.append("")
        lines.append("🚫 Passed (earlier views on these no longer count):")
        lines.extend(
            f"• {item['sport'].upper()} · {item['away_team']} @ "
            f"{item['home_team']} {item['market']}"
            + (f": {item['reasoning']}" if item["reasoning"] else "")
            for item in passes
        )
    if notes:
        if picks or passes:
            lines.append("")
        lines.append(f"📎 Attached {len(notes)} note(s) to games:")
        for note in notes:
            label = (
                "after the result, not counted"
                if note["kind"] == "hindsight"
                else "context"
            )
            lines.append(
                f"• {note['sport'].upper()} · {note['away_team']} @ "
                f"{note['home_team']} ({label}): {note['summary']}"
            )
    return "\n".join(lines)


def short_team(name: str) -> str:
    return name.split()[-1] if name else name


def describe_lines(event: MarketEvent) -> str:
    parts = []
    for team in (event.away_team, event.home_team):
        quote = event.markets.get(f"spread:{team}")
        if quote is not None and quote.point is not None and quote.point < 0:
            parts.append(f"{short_team(team)} {quote.point:+g}")
            break
    else:
        quote = event.markets.get(f"spread:{event.home_team}")
        if quote is not None and quote.point == 0:
            parts.append("pk")
    total = event.markets.get("total:Over")
    if total is not None and total.point is not None:
        parts.append(f"o/u {total.point:g}")
    return ", ".join(parts) or "no BetOnline line yet"


def games_keyboard(events: list[MarketEvent]) -> InlineKeyboardMarkup:
    rows = []
    for event in events:
        start = parse_timestamp(event.commence_time).astimezone(EASTERN)
        label = (
            f"{start:%a %-I:%M%p} {event.sport.upper()} "
            f"{short_team(event.away_team)} @ {short_team(event.home_team)}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    label, callback_data=f"game:{event.sport}:{event.id}"[:64]
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def format_games(events: list[MarketEvent], now: datetime) -> str:
    lines = ["🗓 Today's and tomorrow's games (BetOnline). Tap one for my take:"]
    for event in events:
        start = parse_timestamp(event.commence_time)
        status = " · started" if start <= now else ""
        lines.append(
            f"• {start.astimezone(EASTERN):%a %-I:%M %p} {event.sport.upper()} "
            f"{event.away_team} @ {event.home_team}: {describe_lines(event)}"
            f"{status}"
        )
    return "\n".join(lines)


def format_opinion(event: MarketEvent, opinion: Any) -> str:
    confidence = (
        f" ({opinion.confidence_probability:.0%})"
        if opinion.confidence_probability is not None
        else ""
    )
    lines = [
        f"🎯 {event.sport.upper()} · {event.away_team} @ {event.home_team}",
        f"Lines: {describe_lines(event)}",
        "",
        f"🔍 Your view: {opinion.critique}",
        "",
        f"💡 Mel's call{confidence}: {opinion.recommendation}",
    ]
    if opinion.watch_for:
        lines.append(f"👀 Watch for: {opinion.watch_for}")
    return "\n".join(lines)


def eastern_day(commence_time: str) -> str:
    return parse_timestamp(commence_time).astimezone(EASTERN).date().isoformat()


def actual_outcome(
    decision: dict[str, Any], home_team: str, final: FinalGame | None
) -> dict[str, int] | None:
    """The selection's final margin, or the game's total points."""
    if final is None:
        return None
    if decision["pick_type"] == "total":
        return {"total_points": final.away_score + final.home_score}
    margin = final.home_score - final.away_score
    if not team_matches(decision["selection"], home_team):
        margin = -margin
    return {"selection_margin": margin}


def format_learning(learning: dict[str, Any]) -> str:
    lines = [f"📓 Learnings for {learning['day']}", "", learning["summary"]]

    def bullets(title: str, items: list[str]) -> None:
        if items:
            lines.extend(["", title, *(f"• {item}" for item in items)])

    def para(title: str, text: str) -> None:
        if text:
            lines.extend(["", f"{title} {text}"])

    bullets("✅ Worked", learning["worked"])
    bullets("❌ Didn't work", learning["did_not_work"])
    para("🔢 Your number vs BetOnline:", learning["my_number_vs_market"])
    para("🔄 Flips:", learning["flips"])
    para("🎯 Calibration:", learning["calibration"])
    para("📏 Earlier rules:", learning["earlier_rules_followed"])
    bullets("🧪 Rules to try", learning["rules_to_try"])
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_LIMIT else text[: TELEGRAM_LIMIT - 1] + "…"


def format_stats(
    stats: dict[str, Any],
    bets: dict[str, Any] | None = None,
    flips: dict[str, Any] | None = None,
) -> str:
    lines = [
        "📊 Mel decision report",
        *_stats_section("All decisions (bets and leans)", stats),
    ]
    if bets is not None and bets["total"]:
        lines.extend(["", *_stats_section("Bets only", bets)])
    if flips is not None and flips["final_position_record"] != "0-0-0":
        lines.extend(
            [
                "",
                "— Changing sides —",
                (
                    f"Flips: {flips['flips']} · helped {flips['helped']}, "
                    f"hurt {flips['hurt']}, no difference "
                    f"{flips['no_difference']}"
                ),
                (
                    f"First view {flips['first_view_record']} vs final "
                    f"position {flips['final_position_record']} (W-L-P)"
                ),
            ]
        )
    return "\n".join(lines)


def _stats_section(title: str, stats: dict[str, Any]) -> list[str]:
    decided = stats["wins"] + stats["losses"]
    lines = [
        f"— {title} —",
        (
            f"Record: {stats['wins']}-{stats['losses']}-{stats['pushes']}"
            + (
                f" ({stats['wins'] / decided:.0%})"
                if decided
                else ""
            )
            + f" · open: {stats['open']}"
        ),
    ]
    if stats["brier"] is not None:
        lines.append(
            f"Calibration (Brier, lower is better): {stats['brier']:.3f} "
            f"across {stats['calibrated_count']} pick(s) with stated "
            "confidence; 0.250 is coin-flip guessing"
        )
    else:
        lines.append(
            "Calibration: state a percentage (e.g. “65% on this”) to track it"
        )
    if stats["clv_count"]:
        lines.append(
            f"Beat the closing line: {stats['positive_clv']}/"
            f"{stats['clv_count']} (avg {stats['clv_average']:+.2f})"
        )
    if stats["process_average"] is not None:
        lines.append(f"Average process score: {stats['process_average']:.1f}/5")
    if stats["biases"]:
        lines.append(
            "Recurring bias flags: "
            + ", ".join(f"{tag} ×{count}" for tag, count in stats["biases"])
        )
    return lines


def telegram_audio(message: Any) -> tuple[Any, str, str] | None:
    if message.voice:
        mime = str(message.voice.mime_type or "audio/ogg").lower()
        return message.voice, "voice", AUDIO_SUFFIXES.get(mime, ".ogg")
    if message.audio:
        mime = str(message.audio.mime_type or "audio/mpeg").lower()
        return message.audio, "audio", AUDIO_SUFFIXES.get(mime, ".audio")
    if message.document:
        mime = str(message.document.mime_type or "").lower()
        if mime.startswith("audio/"):
            return (
                message.document,
                "document",
                AUDIO_SUFFIXES.get(mime, ".audio"),
            )
    return None


def with_lines(game: MarketEvent, line: MarketEvent | None) -> MarketEvent:
    """``game`` with BetOnline markets keyed by its own (ESPN) team names."""
    if line is None:
        return game
    names = {line.away_team: game.away_team, line.home_team: game.home_team}
    markets = {}
    for key, quote in line.markets.items():
        market, _, selection = key.partition(":")
        markets[f"{market}:{names.get(selection, selection)}"] = quote
    return replace(game, markets=markets)


class DecisionBot:
    def __init__(
        self,
        config: Config,
        store: DecisionStore,
        *,
        transcriber: WhisperTranscriber,
        claude: ClaudeClient,
        lines: SheetLines,
        espn: EspnClient,
    ) -> None:
        self.config = config
        self.store = store
        self.transcriber = transcriber
        self.claude = claude
        self.lines = lines
        self.espn = espn
        self._processing = asyncio.Lock()
        self._sync = asyncio.Lock()
        self._learning_retry_at: datetime | None = None

    def authorized(self, handler: Handler) -> Handler:
        async def wrapped(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
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
                    "Rejected update user=%s chat=%s",
                    user.id if user else None,
                    chat.type if chat else None,
                )
                return
            await handler(update, context)

        return wrapped

    async def post_init(self, application: Application) -> None:
        self.store.initialize()
        self.config.audio_dir.mkdir(parents=True, exist_ok=True)
        identity = await application.bot.get_me()
        if identity.username != self.config.expected_username:
            raise RuntimeError(
                "Decision helper identity mismatch: expected "
                f"@{self.config.expected_username}, got @{identity.username}"
            )
        await application.bot.set_my_commands(
            [
                BotCommand("games", "Today's and tomorrow's games; get my take"),
                BotCommand("recent", "Show the latest picks and leans"),
                BotCommand("stats", "Decision-quality report"),
                BotCommand("learnings", "Learnings from a day of decisions"),
                BotCommand("reparse", "Re-run parsing on the latest (or replied-to) note"),
                BotCommand("void", "Remove a mis-parsed open pick"),
                BotCommand("help", "How Mel works"),
            ]
        )
        assert application.job_queue is not None
        application.job_queue.run_repeating(
            self.sync_job,
            interval=self.config.sync_interval_seconds,
            first=30,
            name="decision-sync",
        )
        # Load Whisper before the first voice note so it is not slow.
        application.create_task(asyncio.to_thread(self.transcriber.warm))

    async def post_shutdown(self, application: Application) -> None:
        del application
        await self.lines.close()
        await self.espn.close()

    async def start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await update.message.reply_text("👋 " + HELP_TEXT)

    async def help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await update.message.reply_text(HELP_TEXT)

    async def receive_audio(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        detected = telegram_audio(message)
        if detected is None:
            await message.reply_text("Send a voice note or an audio file.")
            return
        media, kind, suffix = detected
        if (
            media.file_size is not None
            and media.file_size > self.config.max_download_bytes
        ):
            await message.reply_text(
                "That audio is larger than Telegram's 20 MB bot download "
                "limit. Split it into shorter voice notes."
            )
            return
        entry, created = self.store.create_entry(
            telegram_chat_id=update.effective_chat.id,
            telegram_message_id=message.message_id,
            audio_kind=kind,
            status="awaiting_choice",
            audio_suffix=suffix,
        )
        if not created:
            return
        audio_path = self.audio_path(entry)
        try:
            telegram_file = await media.get_file()
            await telegram_file.download_to_drive(custom_path=audio_path)
            if audio_path.stat().st_size > self.config.max_download_bytes:
                raise RuntimeError("Downloaded audio exceeds 20 MB")
        except Exception as error:
            log.exception("Download failed for entry %s", entry["id"])
            audio_path.unlink(missing_ok=True)
            detail = str(error) or type(error).__name__
            self.store.fail_entry(entry["id"], detail[:1000])
            await message.reply_text(f"⚠️ I couldn't download that audio: {detail[:500]}")
            return
        await message.reply_text(
            "🎧 Got it. Parse this note or trash the audio?",
            reply_markup=choice_keyboard(entry["id"]),
        )

    def audio_path(self, entry: dict[str, Any]) -> Path:
        return self.config.audio_dir / f"{entry['id']}{entry['audio_suffix']}"

    async def choose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        action, _, entry_id = str(query.data).partition(":")
        await query.answer()
        entry = self.store.choose(entry_id, action)
        if entry is None:
            await query.edit_message_text("This note was already handled.")
            return
        audio_path = self.audio_path(entry)
        if action == "trash":
            audio_path.unlink(missing_ok=True)
            await query.edit_message_text("🗑 Trashed. The audio was deleted.")
            return
        await query.edit_message_text(
            "🧾 Transcribing and parsing your end-of-day summary…"
            if action == "summary"
            else "🧠 Transcribing and parsing…"
        )
        context.application.create_task(
            self._process(entry_id, query.message, audio_path=audio_path)
        )

    async def omitted_bet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        query = update.callback_query
        await query.answer()
        action, _, pick_id = str(query.data).partition(":")
        if action == "keepbet":
            pick = self.store.get_pick(pick_id)
            if pick is None:
                await query.edit_message_text("That bet no longer exists.")
                return
            await query.edit_message_text(f"✅ Kept as a bet:\n{describe_pick(pick)}")
            return
        try:
            pick = self.store.make_lean(pick_id)
        except LookupError as error:
            await query.edit_message_text(str(error))
            return
        await query.edit_message_text(f"↩️ Changed to a lean:\n{describe_pick(pick)}")

    async def remove(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        query = update.callback_query
        await query.answer()
        entry_id = str(query.data).partition(":")[2]
        try:
            picks = self.store.remove_entry(entry_id)
        except ValueError as error:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(str(error))
            return
        self.audio_path(self.store.get_entry(entry_id)).unlink(missing_ok=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"🗑 Removed that note and its {len(picks)} pick(s)/lean(s). "
            "Send it again with what you meant."
        )

    async def reparse(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        replied = update.message.reply_to_message
        if replied is not None:
            target = self.store.entry_for_message(
                update.effective_chat.id, replied.message_id
            )
            if target is None:
                await update.message.reply_text(
                    "Reply /reparse to one of your own voice or text notes."
                )
                return
        else:
            target = self.store.latest_entry()
            if target is None:
                await update.message.reply_text("There is no saved note to reparse.")
                return
        await self._reparse(target["id"], update.message, context)

    async def reparse_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        await self._reparse(
            str(query.data).partition(":")[2], query.message, context
        )

    async def _reparse(
        self, entry_id: str, message: Any, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            entry = self.store.reopen_entry(entry_id)
        except (LookupError, ValueError) as error:
            await message.reply_text(str(error))
            return
        await message.reply_text("🔁 Reparsing that note…")
        context.application.create_task(
            self._process(entry["id"], message, text=entry["transcript"])
        )

    async def receive_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        summary = bool(SUMMARY_TEXT.match(message.text or ""))
        entry, created = self.store.create_entry(
            telegram_chat_id=update.effective_chat.id,
            telegram_message_id=message.message_id,
            audio_kind="text",
            mode="summary" if summary else "note",
        )
        if not created:
            return
        await message.reply_text(
            "🧾 Parsing your end-of-day summary…" if summary else "📝 Parsing…"
        )
        context.application.create_task(
            self._process(entry["id"], message, text=message.text)
        )

    async def _process(
        self,
        entry_id: str,
        message: Any,
        *,
        audio_path: Path | None = None,
        text: str | None = None,
    ) -> None:
        async with self._processing:
            try:
                if text is None:
                    transcript = await asyncio.to_thread(
                        self.transcriber.transcribe, audio_path
                    )
                    # Keep the words even if parsing fails, so /reparse works.
                    self.store.set_transcript(entry_id, transcript)
                else:
                    transcript = text
                recorded_at = parse_timestamp(
                    self.store.get_entry(entry_id)["created_at"]
                )
                entry = self.store.get_entry(entry_id)
                clarification = entry["clarification"]
                summary = entry["mode"] == "summary"
                # Reparsed or late-parsed notes use the lines from when they
                # were recorded, not today's.
                historic = utc_now() - recorded_at > HISTORIC_LINES_AFTER
                events = await self.candidate_events(
                    lines_as_of=recorded_at if historic else None
                )
                if not events:
                    raise RuntimeError(
                        "No recent or upcoming NFL or NBA games were "
                        "available to match your picks"
                    )
                parse = self.claude.summary if summary else self.claude.extract
                extraction = await parse(
                    transcript,
                    events,
                    recorded_at=recorded_at,
                    prior_views=self.store.prior_views(events, before=recorded_at),
                    clarification=clarification,
                )
                picks = self.store.save_extraction(
                    entry_id=entry_id,
                    transcript=transcript,
                    extraction=extraction,
                    events=events,
                    logged_at=recorded_at,
                    refresh_market=historic,
                )
                reply = format_saved(
                    picks,
                    self.store.entry_notes(entry_id),
                    transcript,
                    self.store.entry_passes(entry_id),
                )
                if summary:
                    day = extraction.summary_day or max(
                        (eastern_day(pick["commence_time"]) for pick in picks),
                        default=(recorded_at - SUMMARY_DAY_ROLLOVER)
                        .astimezone(EASTERN)
                        .date()
                        .isoformat(),
                    )
                    reply = f"🧾 End-of-day summary for {day}\n\n{reply}"
                await message.reply_text(
                    reply, reply_markup=remove_keyboard(entry_id)
                )
                if summary:
                    for pick in self.store.omitted_bets(entry_id, day):
                        await message.reply_text(
                            "❓ Your end-of-day summary didn't mention this "
                            f"bet:\n{describe_pick(pick)}\nIs it still a bet?",
                            reply_markup=omitted_keyboard(pick["id"]),
                        )
            except Exception as error:
                log.exception("Entry %s failed", entry_id)
                detail = str(error) or type(error).__name__
                self.store.fail_entry(entry_id, detail[:1000])
                await message.reply_text(
                    f"⚠️ I couldn't save that one: {detail[:500]}"
                )

    async def candidate_events(
        self,
        lines_as_of: datetime | None = None,
        day_offsets: range | None = None,
    ) -> list[MarketEvent]:
        """ESPN games from the last few days through the next week, with
        BetOnline lines from the forwarder's sheet (no paid API calls).

        ``lines_as_of`` swaps in the lines BetOnline showed at that time, and
        centers the window on it (a reparsed older note).
        ``day_offsets`` limits the ESPN days (ET, relative to today).
        """
        now = lines_as_of or utc_now()
        today = now.astimezone(EASTERN).date()
        offsets = day_offsets or range(
            1 - STARTED_LOOKBACK_DAYS, CANDIDATE_WINDOW.days + 1
        )
        dates = [
            (today + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in offsets
        ]
        events: list[MarketEvent] = []
        failures: list[str] = []
        for sport in ("nfl", "nba"):
            try:
                sheet = await self.lines.fetch(sport, as_of=lines_as_of)
            except Exception as error:
                log.warning("Lines sheet read failed for %s: %s", sport, error)
                failures.append(f"{sport.upper()} lines: {error}")
                sheet = []
            results = await asyncio.gather(
                *(self.espn.fetch_events(sport, date) for date in dates),
                return_exceptions=True,
            )
            games: dict[str, MarketEvent] = {}
            for date, result in zip(dates, results):
                if isinstance(result, BaseException):
                    log.warning("ESPN fetch failed for %s %s: %s", sport, date, result)
                    failures.append(f"{sport.upper()} {date}: {result}")
                    continue
                for game in result:
                    if parse_timestamp(game.commence_time) <= now + CANDIDATE_WINDOW:
                        games[game.id] = game
            matched: set[str] = set()
            for game in games.values():
                line = match_event(
                    sheet,
                    event_id="",
                    away_team=game.away_team,
                    home_team=game.home_team,
                    commence_time=game.commence_time,
                )
                if line is not None:
                    matched.add(line.id)
                events.append(with_lines(game, line))
            # Sheet games ESPN didn't return (an ESPN outage) stay pickable.
            events.extend(
                line
                for line in sheet
                if line.id not in matched
                and now < parse_timestamp(line.commence_time) <= now + CANDIDATE_WINDOW
            )
        if not events and failures:
            raise RuntimeError("Game data unavailable (" + "; ".join(failures[:4]) + ")")
        return sorted(events, key=lambda event: event.commence_time)

    async def sync_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self._sync.locked():
            return
        async with self._sync:
            for step in (
                self.sync_markets,
                self.sync_results,
                self.sync_reviews,
                self.sync_learnings,
            ):
                try:
                    await step()
                except Exception:
                    log.exception("%s failed", step.__name__)

    async def sync_markets(self) -> None:
        due = self.store.due_market_picks()
        for sport in sorted({pick["sport"] for pick in due}):
            events = await self.lines.fetch(sport)
            for pick in (item for item in due if item["sport"] == sport):
                event = match_event(
                    events,
                    event_id=pick["event_id"],
                    away_team=pick["away_team"],
                    home_team=pick["home_team"],
                    commence_time=pick["commence_time"],
                )
                self.store.record_market_snapshot(pick["id"], event)

    async def sync_results(self) -> None:
        due = self.store.due_result_picks()
        cache: dict[tuple[str, str], list[Any]] = {}
        for pick in due:
            date = (
                parse_timestamp(pick["commence_time"])
                .astimezone(EASTERN)
                .strftime("%Y%m%d")
            )
            key = (pick["sport"], date)
            if key not in cache:
                cache[key] = await self.espn.fetch_finals(*key)
            game = match_final(
                cache[key],
                away_team=pick["away_team"],
                home_team=pick["home_team"],
                commence_time=pick["commence_time"],
            )
            if game is None:
                self.store.defer_result(pick["id"])
            else:
                self.store.settle_pick(pick["id"], game)

    async def sync_reviews(self) -> None:
        """Grade each final decision's process. Reviews are stored for
        /stats and opinions, not sent."""
        for pick in self.store.due_analysis():
            record = {
                key: pick[key]
                for key in (
                    "sport",
                    "away_team",
                    "home_team",
                    "pick_type",
                    "selection",
                    "line",
                    "line_source",
                    "price_at_pick",
                    "spread_team",
                    "spread_at_pick",
                    "my_line_low",
                    "my_line_high",
                    "confidence_probability",
                    "confidence_label",
                    "reasoning",
                    "commitment",
                    "logged_after_start",
                    "result",
                    "away_score",
                    "home_score",
                    "closing_line",
                    "closing_price",
                    "closing_value",
                )
            }
            path = self.store.decision_path(pick["id"])
            record["path"] = [
                {
                    "pick_type": step["pick_type"],
                    "selection": step["selection"],
                    "line": step["line"],
                    "commitment": step["commitment"],
                    "logged_at": step["created_at"],
                    "reasoning": step["reasoning"],
                    "result_if_kept": step["result"],
                }
                for step in path
            ]
            record["logged_at"] = pick["created_at"]
            record["from_end_of_day_summary"] = pick.get("entry_mode") == "summary"
            record["commence_time"] = pick["commence_time"]
            record["uncertainties"] = json.loads(pick["uncertainties_json"])
            game = self.store.game_record(
                pick["sport"],
                pick["away_team"],
                pick["home_team"],
                pick["commence_time"],
            )
            record["all_notes_on_game"] = game["notes"] or [
                {"logged_at": pick["created_at"], "transcript": pick["transcript"]}
            ]
            record["game_notes"] = game["game_notes"]
            record["recent_daily_rules"] = [
                {"day": item["day"], "rules_to_try": item["rules_to_try"]}
                for item in self.store.recent_learnings(
                    3, before_day=eastern_day(pick["commence_time"])
                )
            ]
            try:
                review = await self.claude.review(record)
            except Exception as error:
                log.warning("Review failed for %s: %s", pick["id"], error)
                self.store.defer_analysis(pick["id"])
                continue
            self.store.complete_analysis(pick["id"], review)

    def _today_and_tomorrow(
        self, events: list[MarketEvent]
    ) -> list[MarketEvent]:
        today = utc_now().astimezone(EASTERN).date()
        days = {today, today + timedelta(days=1)}
        return [
            event
            for event in events
            if parse_timestamp(event.commence_time).astimezone(EASTERN).date()
            in days
        ]

    async def games(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        try:
            events = self._today_and_tomorrow(
                await self.candidate_events(day_offsets=range(0, 2))
            )
        except Exception as error:
            log.exception("Games list failed")
            await update.message.reply_text(f"⚠️ Couldn't load games: {error}")
            return
        if not events:
            await update.message.reply_text(
                "No NFL or NBA games today or tomorrow."
            )
            return
        await update.message.reply_text(
            format_games(events, utc_now()),
            reply_markup=games_keyboard(events),
        )

    async def choose_game(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        _, sport, event_id = str(query.data).split(":", 2)
        context.application.create_task(
            self._opinion(sport, event_id, query.message)
        )

    async def _opinion(self, sport: str, event_id: str, message: Any) -> None:
        try:
            events = await self.candidate_events(day_offsets=range(0, 2))
            event = next(
                (
                    item
                    for item in events
                    if item.sport == sport and item.id.startswith(event_id)
                ),
                None,
            )
            if event is None:
                await message.reply_text(
                    "That game is no longer in today's or tomorrow's list."
                )
                return
            thinking = await message.reply_text(
                f"🤔 Thinking about {event.away_team} @ {event.home_team}…"
            )
            opinion = await self.claude.opinion(
                await self.opinion_context(event)
            )
            await thinking.edit_text(format_opinion(event, opinion))
        except Exception as error:
            log.exception("Opinion failed for %s %s", sport, event_id)
            detail = str(error) or type(error).__name__
            await message.reply_text(f"⚠️ I couldn't form an opinion: {detail[:500]}")

    async def opinion_context(self, event: MarketEvent) -> dict[str, Any]:
        game = event.prompt_dict()
        game["already_started"] = parse_timestamp(event.commence_time) <= utc_now()
        try:
            opening = match_event(
                await self.lines.fetch_opening(event.sport),
                event_id="",
                away_team=event.away_team,
                home_team=event.home_team,
                commence_time=event.commence_time,
            )
        except Exception as error:
            log.warning("Opening lines unavailable: %s", error)
            opening = None
        if opening is not None:
            game["betonline_opening_lines"] = with_lines(
                replace(event, markets={}), opening
            ).prompt_dict()["betonline_lines"]
        return {
            "game": game,
            "my_views": self.store.game_record(
                event.sport, event.away_team, event.home_team, event.commence_time
            ),
            "track_record": {
                "all_decisions": self.store.stats(None),
                "bets": self.store.stats("pick"),
                "side_changes": self.store.flip_stats(),
                "recent_lessons": self.store.recent_lessons(),
                "daily_learnings": [
                    {
                        key: item[key]
                        for key in ("day", "summary", "did_not_work", "rules_to_try")
                    }
                    for item in self.store.recent_learnings(3)
                ],
            },
        }

    def due_learning_day(self, now: datetime | None = None) -> str | None:
        """The oldest Eastern day with thoughts but no learnings, once it is
        past 9 ET the next morning and every pick is graded and reviewed (or
        a day later, if no review is still retrying). Days run oldest first."""
        current = (now or utc_now()).astimezone(EASTERN)
        done = self.store.learning_days()
        days: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.discussed_games():
            days.setdefault(eastern_day(row["commence_time"]), []).append(row)
        for day in sorted(days):
            if day in done:
                continue
            ready_at = datetime.combine(
                date.fromisoformat(day) + timedelta(days=1),
                time(LEARNING_HOUR),
                EASTERN,
            )
            if current < ready_at:
                return None
            settled = all(
                row["status"] == "final" and row["analysis_status"] != "pending"
                for row in days[day]
            )
            reviewing = any(
                row["analysis_status"] == "pending"
                and row["analysis_attempts"] < LEARNING_REVIEW_ATTEMPTS
                for row in days[day]
            )
            overdue = current >= ready_at + timedelta(days=1) and not reviewing
            return day if settled or overdue else None
        return None

    async def sync_learnings(self) -> None:
        """Write one day's learnings per tick, so past days backfill in order.
        Stored for /learnings, opinions, and reviews; never sent."""
        now = utc_now()
        if self._learning_retry_at is not None and now < self._learning_retry_at:
            return
        day = self.due_learning_day(now)
        if day is None:
            return
        try:
            learning = await self.claude.learnings(await self.learning_context(day))
        except Exception:
            self._learning_retry_at = now + LEARNING_RETRY
            raise
        self._learning_retry_at = None
        self.store.save_learning(day, learning.as_dict())
        log.info("Saved daily learnings for %s", day)

    async def learning_context(self, day: str) -> dict[str, Any]:
        starts: dict[tuple[str, str, str], str] = {}
        for row in self.store.discussed_games():
            if eastern_day(row["commence_time"]) == day:
                starts.setdefault(
                    (row["sport"], row["away_team"], row["home_team"]),
                    row["commence_time"],
                )
        finals: dict[str, list[FinalGame]] = {}
        for sport in sorted({key[0] for key in starts}):
            try:
                finals[sport] = await self.espn.fetch_finals(
                    sport, day.replace("-", "")
                )
            except Exception as error:
                log.warning("Finals unavailable for %s %s: %s", sport, day, error)
                finals[sport] = []
        games = []
        transcripts: dict[tuple[str, str], dict[str, str]] = {}
        record = {"all": [0, 0, 0], "bets": [0, 0, 0]}
        for (sport, away, home), start in sorted(starts.items(), key=lambda item: item[1]):
            game = self.store.game_record(sport, away, home, start, detailed=True)
            # One note often covers several games; list each transcript once.
            for note in game.pop("notes"):
                transcripts.setdefault((note["logged_at"], note["transcript"]), note)
            final = match_final(
                finals[sport], away_team=away, home_team=home, commence_time=start
            )
            for decision in game["decisions"]:
                decision["actual"] = actual_outcome(decision, home, final)
                if decision["role"] == "final":
                    buckets = [record["all"]]
                    if decision["commitment"] == "pick":
                        buckets.append(record["bets"])
                    for bucket in buckets:
                        for index, result in enumerate(("win", "loss", "push")):
                            bucket[index] += decision["result"] == result
            games.append(
                {
                    "sport": sport,
                    "game": f"{away} @ {home}",
                    "start": start,
                    "final_score": (
                        {away: final.away_score, home: final.home_score}
                        if final is not None
                        else None
                    ),
                    **game,
                }
            )
        return {
            "day": day,
            "games": games,
            "transcripts": sorted(
                transcripts.values(), key=lambda note: note["logged_at"]
            ),
            "day_record": {
                name: "{}-{}-{}".format(*counts) for name, counts in record.items()
            },
            "prior_days": self.store.recent_learnings(7, before_day=day),
            "track_record": {
                "all_decisions": self.store.stats(None),
                "bets": self.store.stats("pick"),
                "side_changes": self.store.flip_stats(),
            },
        }

    async def learnings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        day = context.args[0] if context.args else None
        if day is not None:
            try:
                day = date.fromisoformat(day).isoformat()
            except ValueError:
                await update.message.reply_text("Usage: /learnings [YYYY-MM-DD]")
                return
        learning = self.store.learning(day)
        if learning is None:
            await update.message.reply_text(
                f"No learnings for {day} yet."
                if day
                else "No daily learnings yet; the first comes the morning "
                "after a day with notes."
            )
            return
        await update.message.reply_text(format_learning(learning))

    async def recent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        picks = self.store.recent()
        if not picks:
            await update.message.reply_text("No picks saved yet.")
            return
        lines = []
        for pick in picks:
            status = (
                str(pick["result"]).upper()
                if pick["status"] == "final"
                else pick["status"]
            )
            lines.append(f"[{status}] {describe_pick(pick)}")
        await update.message.reply_text("\n".join(lines))

    async def stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await update.message.reply_text(
            format_stats(
                self.store.stats(None),
                self.store.stats("pick"),
                self.store.flip_stats(),
            )
        )

    async def void(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /void <id-prefix>")
            return
        try:
            pick = self.store.void(context.args[0])
        except (LookupError, ValueError) as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(f"Voided {describe_pick(pick)}")

    def application(self) -> Application:
        application = (
            Application.builder()
            .token(self.config.bot_token)
            .concurrent_updates(True)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        for name, handler in (
            ("start", self.start),
            ("help", self.help),
            ("games", self.games),
            ("recent", self.recent),
            ("stats", self.stats),
            ("learnings", self.learnings),
            ("reparse", self.reparse),
            ("void", self.void),
        ):
            application.add_handler(
                CommandHandler(name, self.authorized(handler))
            )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.choose), pattern=r"^(parse|trash|summary):"
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.omitted_bet), pattern=r"^(keepbet|waslean):"
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.choose_game), pattern=r"^game:"
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.remove), pattern=r"^remove:"
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.reparse_button), pattern=r"^reparse:"
            )
        )
        application.add_handler(
            MessageHandler(
                filters.VOICE | filters.AUDIO | filters.Document.AUDIO,
                self.authorized(self.receive_audio),
            )
        )
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.authorized(self.receive_text),
            )
        )
        return application


def main() -> None:
    config = Config.from_env()
    bot = DecisionBot(
        config,
        DecisionStore(config.database_path),
        transcriber=WhisperTranscriber(config.whisper_model, config.model_dir),
        claude=ClaudeClient(
            config.claude_binary,
            config.claude_model,
            config.claude_effort,
            config.claude_timeout_seconds,
            config.claude_workdir,
        ),
        lines=SheetLines(config.google_credentials, config.lines_sheet_id),
        espn=EspnClient(),
    )
    bot.application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
