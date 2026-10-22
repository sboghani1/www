from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
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
from .models import MarketEvent, parse_timestamp, utc_now
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
    "parse it or trash it. Committed picks and tentative leans are tracked "
    "separately; I follow the market until kickoff/tip and review your "
    "process after each game.\n\n"
    "/recent - latest 10 picks and leans\n"
    "/stats - record, calibration, closing-line value, process, biases\n"
    "/reparse - re-run parsing on your latest note\n"
    "/void <id-prefix> - remove a mis-parsed open pick"
)


def choice_keyboard(entry_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧠 Parse", callback_data=f"parse:{entry_id}"),
                InlineKeyboardButton("🗑 Trash", callback_data=f"trash:{entry_id}"),
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
    confidence = ""
    if pick["confidence_probability"] is not None:
        confidence = f" · {pick['confidence_probability']:.0%}"
    elif pick["confidence_label"]:
        confidence = f" · {pick['confidence_label']}"
    label = "Lean · " if pick.get("commitment") == "lean" else ""
    return (
        f"{pick['id'][:8]} · {label}{pick['sport'].upper()} · {matchup}: "
        f"{detail}{confidence}"
    )


def format_saved(
    picks: list[dict[str, Any]],
    feedback: str,
    transcript: str,
) -> str:
    excerpt = " ".join(transcript.split())
    if len(excerpt) > 600:
        excerpt = excerpt[:597] + "..."
    lines = [f"📝 Heard: “{excerpt}”", ""]
    if picks:
        committed = sum(pick.get("commitment") != "lean" for pick in picks)
        counts = []
        if committed:
            counts.append(f"{committed} pick(s)")
        if len(picks) - committed:
            counts.append(f"{len(picks) - committed} lean(s)")
        lines.append(f"✅ Saved {' and '.join(counts)}:")
        lines.extend(f"• {describe_pick(pick)}" for pick in picks)
    else:
        lines.append(
            "No NFL/NBA winner, spread, or total picks or leans were found "
            "for upcoming games."
        )
    if feedback:
        lines.extend(["", f"🧠 {feedback}"])
    return "\n".join(lines)


def format_clv(pick: dict[str, Any]) -> str:
    value = pick["closing_value"]
    if value is None:
        return "Closing-line value: unavailable"
    if pick["pick_type"] == "winner":
        return f"Closing-line value: {value * 100:+.1f} pts implied probability"
    return f"Closing-line value: {value:+g} points"


def format_review(pick: dict[str, Any]) -> str:
    result = str(pick["result"]).upper()
    tags = json.loads(pick["bias_tags_json"])
    lines = [
        f"🏁 {result}: {describe_pick(pick)}",
        (
            f"Final: {pick['away_team']} {pick['away_score']} - "
            f"{pick['home_team']} {pick['home_score']}"
        ),
        format_clv(pick),
        f"Process score: {pick['process_score']}/5",
        f"Luck vs. skill: {pick['luck_summary']}",
        f"✅ Worked: {pick['what_worked']}",
        f"🔧 Improve: {pick['what_to_improve']}",
    ]
    if tags:
        lines.append("Bias flags: " + ", ".join(tags))
    return "\n".join(lines)


def format_stats(
    stats: dict[str, Any], leans: dict[str, Any] | None = None
) -> str:
    lines = ["📊 Mel decision report", *_stats_section("Committed picks", stats)]
    if leans is not None and leans["total"]:
        lines.extend(["", *_stats_section("Leans (tracked separately)", leans)])
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
                BotCommand("recent", "Show the latest picks and leans"),
                BotCommand("stats", "Decision-quality report"),
                BotCommand("reparse", "Re-run parsing on the latest note"),
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
        await query.edit_message_text("🧠 Transcribing and parsing…")
        context.application.create_task(
            self._process(entry_id, query.message, audio_path=audio_path)
        )

    async def reparse(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        latest = self.store.latest_entry()
        if latest is None:
            await update.message.reply_text("There is no saved note to reparse.")
            return
        try:
            entry = self.store.reopen_entry(latest["id"])
        except (LookupError, ValueError) as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text("🔁 Reparsing your latest note…")
        context.application.create_task(
            self._process(entry["id"], update.message, text=entry["transcript"])
        )

    async def receive_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        entry, created = self.store.create_entry(
            telegram_chat_id=update.effective_chat.id,
            telegram_message_id=message.message_id,
            audio_kind="text",
        )
        if not created:
            return
        await message.reply_text("📝 Parsing…")
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
                events = await self.candidate_events()
                if not events:
                    raise RuntimeError(
                        "No recent or upcoming NFL or NBA games were "
                        "available to match your picks"
                    )
                extraction = await self.claude.extract(transcript, events)
                picks = self.store.save_extraction(
                    entry_id=entry_id,
                    transcript=transcript,
                    extraction=extraction,
                    events=events,
                )
                await message.reply_text(
                    format_saved(
                        picks, extraction.immediate_feedback, transcript
                    )
                )
            except Exception as error:
                log.exception("Entry %s failed", entry_id)
                detail = str(error) or type(error).__name__
                self.store.fail_entry(entry_id, detail[:1000])
                await message.reply_text(
                    f"⚠️ I couldn't save that one: {detail[:500]}"
                )
            finally:
                if audio_path is not None:
                    audio_path.unlink(missing_ok=True)

    async def candidate_events(self) -> list[MarketEvent]:
        """ESPN games from the last few days through the next week, with
        BetOnline lines from the forwarder's sheet (no paid API calls)."""
        now = utc_now()
        today = now.astimezone(EASTERN).date()
        dates = [
            (today + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(
                1 - STARTED_LOOKBACK_DAYS, CANDIDATE_WINDOW.days + 1
            )
        ]
        events: list[MarketEvent] = []
        failures: list[str] = []
        for sport in ("nfl", "nba"):
            try:
                sheet = await self.lines.fetch(sport)
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
        if self._sync.locked():
            return
        async with self._sync:
            for step in (self.sync_markets, self.sync_results):
                try:
                    await step()
                except Exception:
                    log.exception("%s failed", step.__name__)
            try:
                await self.sync_reviews(context.bot)
            except Exception:
                log.exception("sync_reviews failed")

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

    async def sync_reviews(self, bot: Any) -> None:
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
            record["uncertainties"] = json.loads(pick["uncertainties_json"])
            record["full_transcript"] = pick["transcript"]
            try:
                review = await self.claude.review(record)
            except Exception as error:
                log.warning("Review failed for %s: %s", pick["id"], error)
                self.store.defer_analysis(pick["id"])
                continue
            updated = self.store.complete_analysis(pick["id"], review)
            await bot.send_message(
                chat_id=updated["telegram_chat_id"],
                text=format_review(updated),
            )
            self.store.mark_feedback_sent(pick["id"])

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
            format_stats(self.store.stats(), self.store.stats("lean"))
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
            ("recent", self.recent),
            ("stats", self.stats),
            ("reparse", self.reparse),
            ("void", self.void),
        ):
            application.add_handler(
                CommandHandler(name, self.authorized(handler))
            )
        application.add_handler(
            CallbackQueryHandler(
                self.authorized(self.choose), pattern=r"^(parse|trash):"
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
