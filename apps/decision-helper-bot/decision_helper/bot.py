from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude import ClaudeClient
from .config import Config
from .espn import EspnClient, match_final
from .markets import OddsClient, match_event
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
    "I'm Mel. Send a voice note (or text) with your NFL/NBA thoughts and "
    "picks — winners, spreads, and totals. I save every pick automatically, "
    "give quick feedback, track the market until kickoff/tip, and review "
    "your process after each game.\n\n"
    "/recent - latest 10 picks\n"
    "/stats - record, calibration, closing-line value, process, biases\n"
    "/void <id-prefix> - remove a mis-parsed open pick"
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
        detail += " [market line]"
    if pick["price_at_pick"] is not None:
        detail += f" ({pick['price_at_pick']:+d})"
    confidence = ""
    if pick["confidence_probability"] is not None:
        confidence = f" · {pick['confidence_probability']:.0%}"
    elif pick["confidence_label"]:
        confidence = f" · {pick['confidence_label']}"
    return (
        f"{pick['id'][:8]} · {pick['sport'].upper()} · {matchup}: "
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
        lines.append(f"✅ Saved {len(picks)} pick(s):")
        lines.extend(f"• {describe_pick(pick)}" for pick in picks)
    else:
        lines.append(
            "No gradeable NFL/NBA winner, spread, or total picks were found "
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


def format_stats(stats: dict[str, Any]) -> str:
    decided = stats["wins"] + stats["losses"]
    lines = [
        "📊 Mel decision report",
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
    return "\n".join(lines)


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


class DecisionBot:
    def __init__(
        self,
        config: Config,
        store: DecisionStore,
        *,
        transcriber: WhisperTranscriber,
        claude: ClaudeClient,
        odds: OddsClient,
        espn: EspnClient,
    ) -> None:
        self.config = config
        self.store = store
        self.transcriber = transcriber
        self.claude = claude
        self.odds = odds
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
                BotCommand("recent", "Show the latest picks"),
                BotCommand("stats", "Decision-quality report"),
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
        await self.odds.close()
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
        )
        if not created:
            return
        await message.reply_text("🎧 Got it. Transcribing and parsing…")
        context.application.create_task(
            self._process(entry["id"], message, media=media, suffix=suffix)
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
        media: Any = None,
        suffix: str = "",
        text: str | None = None,
    ) -> None:
        audio_path: Path | None = None
        async with self._processing:
            try:
                if text is None:
                    audio_path = self.config.audio_dir / f"{entry_id}{suffix}"
                    telegram_file = await media.get_file()
                    await telegram_file.download_to_drive(
                        custom_path=audio_path
                    )
                    if audio_path.stat().st_size > self.config.max_download_bytes:
                        raise RuntimeError("Downloaded audio exceeds 20 MB")
                    transcript = await asyncio.to_thread(
                        self.transcriber.transcribe, audio_path
                    )
                else:
                    transcript = text
                events = await self.candidate_events()
                if not events:
                    raise RuntimeError(
                        "No upcoming NFL or NBA games with market data were "
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
        now = utc_now()
        events: list[MarketEvent] = []
        failures: list[str] = []
        for sport in ("nfl", "nba"):
            try:
                fetched = await self.odds.fetch(sport)
            except Exception as error:
                log.warning("Odds fetch failed for %s: %s", sport, error)
                failures.append(f"{sport.upper()}: {error}")
                continue
            events.extend(
                event
                for event in fetched
                if now
                < parse_timestamp(event.commence_time)
                <= now + CANDIDATE_WINDOW
            )
        if not events and failures:
            raise RuntimeError("Market data unavailable (" + "; ".join(failures) + ")")
        return events

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
            events = await self.odds.fetch(sport)
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
        await update.message.reply_text(format_stats(self.store.stats()))

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
            ("void", self.void),
        ):
            application.add_handler(
                CommandHandler(name, self.authorized(handler))
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
        odds=OddsClient(config.odds_api_key),
        espn=EspnClient(),
    )
    bot.application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
