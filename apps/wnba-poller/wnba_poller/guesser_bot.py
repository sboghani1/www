from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .models import ET, NO_DATA, parse_timestamp
from .sheets import SheetsStore

log = logging.getLogger("wnba_poller.guesser")

EXPECTED_BOT_USERNAME = "wnbaguesser_bot"
PAGE_SIZE = 6
SELECTION_TTL = timedelta(minutes=15)
ALLOWLIST_CACHE_SECONDS = 60
MAX_CALLBACK_BYTES = 64
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PERIOD_LABELS = {
    "game": "Full game",
    "first_half": "First half",
}
MARKETS_BY_PERIOD = {
    "game": ("spread", "moneyline", "total"),
    "first_half": ("spread", "total"),
}

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


@dataclass(frozen=True)
class GuesserConfig:
    bot_token: str
    expected_username: str
    sheet_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "GuesserConfig":
        token = os.getenv("WNBA_GUESSER_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("WNBA_GUESSER_BOT_TOKEN is required")
        expected = os.getenv(
            "WNBA_GUESSER_EXPECTED_USERNAME",
            EXPECTED_BOT_USERNAME,
        ).strip().lstrip("@")
        if expected != EXPECTED_BOT_USERNAME:
            raise ValueError(
                "WNBA_GUESSER_EXPECTED_USERNAME must be wnbaguesser_bot"
            )
        timeout = int(
            os.getenv("WNBA_GUESSER_SHEET_TIMEOUT_SECONDS", "45")
        )
        if timeout <= 0 or timeout > 120:
            raise ValueError(
                "WNBA_GUESSER_SHEET_TIMEOUT_SECONDS must be 1-120"
            )
        return cls(
            bot_token=token,
            expected_username=expected,
            sheet_timeout_seconds=timeout,
        )


def selection_id(game: Mapping[str, Any]) -> str:
    value = str(game.get("event_id") or game.get("espn_event_id") or "")
    return value if EVENT_ID_PATTERN.fullmatch(value) else ""


def callback_data(action: str, value: str = "") -> str:
    data = f"{action}:{value}" if value else action
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("callback data exceeds Telegram's 64-byte limit")
    return data


def select_games(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    cutoff = now.astimezone(UTC) + timedelta(days=14)
    selected: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        commence = str(record.get("commence_time_utc") or "")
        if not commence or not selection_id(record):
            continue
        try:
            tip = parse_timestamp(commence)
        except ValueError:
            continue
        if now.astimezone(UTC) < tip <= cutoff:
            selected.append((tip, dict(record)))
    selected.sort(key=lambda item: item[0])
    return [game for _, game in selected]


def page_games(
    games: Sequence[dict[str, Any]],
    page: int,
) -> tuple[list[dict[str, Any]], int, int]:
    page_count = max(1, (len(games) + PAGE_SIZE - 1) // PAGE_SIZE)
    normalized = min(max(page, 0), page_count - 1)
    start = normalized * PAGE_SIZE
    return list(games[start : start + PAGE_SIZE]), normalized, page_count


def game_button_label(game: Mapping[str, Any]) -> str:
    tip = parse_timestamp(str(game["commence_time_utc"])).astimezone(ET)
    return (
        f"{tip:%a %-m/%-d %-I:%M %p} · "
        f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
    )[:90]


def game_browser(
    records: Sequence[Mapping[str, Any]],
    *,
    page: int,
    now: datetime,
) -> tuple[str, InlineKeyboardMarkup]:
    games = select_games(records, now=now)
    current, page, page_count = page_games(games, page)
    lines = [
        "🏀 WNBA games in the next 14 days",
        f"{len(games)} game{'s' if len(games) != 1 else ''} available",
        f"Page {page + 1} of {page_count}",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for game in current:
        rows.append(
            [
                InlineKeyboardButton(
                    game_button_label(game),
                    callback_data=callback_data(
                        "g", f"{page}:{selection_id(game)}"
                    ),
                )
            ]
        )
    if not current:
        lines.append("\nNo upcoming WNBA games are available.")
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀ Prev",
                callback_data=callback_data("games", str(page - 1)),
            )
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                "Next ▶",
                callback_data=callback_data("games", str(page + 1)),
            )
        )
    if navigation:
        rows.append(navigation)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _stored(value: Any) -> Any:
    return NO_DATA if value in ("", None) else value


def _signed(value: Any) -> str:
    if value in ("", None, NO_DATA):
        return NO_DATA
    try:
        number = float(str(value))
    except ValueError:
        return str(value)
    rendered = str(int(number)) if number.is_integer() else str(number)
    return f"+{rendered}" if number > 0 else rendered


def _line(value: Any) -> str:
    if value in ("", None, NO_DATA):
        return NO_DATA
    try:
        number = float(str(value))
    except ValueError:
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


def _market_field(period: str, field: str) -> str:
    return field if period == "game" else f"first_half_{field}"


def period_values(
    game: Mapping[str, Any],
    *,
    period: str,
    snapshot: str,
) -> dict[str, Any]:
    if period not in PERIOD_LABELS or snapshot not in {"opening", "latest"}:
        raise ValueError("invalid line selection")
    fields = (
        "away_spread",
        "away_spread_price",
        "home_spread",
        "home_spread_price",
        "total",
        "over_price",
        "under_price",
    )
    values = {
        field: _stored(
            game.get(f"{snapshot}_{_market_field(period, field)}")
        )
        for field in fields
    }
    values["away_moneyline"] = (
        _stored(game.get(f"{snapshot}_away_moneyline"))
        if period == "game"
        else NO_DATA
    )
    values["home_moneyline"] = (
        _stored(game.get(f"{snapshot}_home_moneyline"))
        if period == "game"
        else NO_DATA
    )
    return values


def _period_lines(
    game: Mapping[str, Any],
    *,
    period: str,
) -> str:
    away = html.escape(str(game.get("away_team") or "Away"))
    home = html.escape(str(game.get("home_team") or "Home"))

    def snapshot(label: str, values: Mapping[str, Any]) -> str:
        rows = [
            f"<u>Spread</u>: {away} {_signed(values['away_spread'])} "
            f"({_signed(values['away_spread_price'])}) · "
            f"{home} {_signed(values['home_spread'])} "
            f"({_signed(values['home_spread_price'])})",
        ]
        if period == "game":
            rows.append(
                f"<u>Moneyline</u>: {away} "
                f"{_signed(values['away_moneyline'])} · "
                f"{home} {_signed(values['home_moneyline'])}"
            )
        rows.append(
            f"<u>Total</u>: {_line(values['total'])} "
            f"(O {_signed(values['over_price'])} / "
            f"U {_signed(values['under_price'])})"
        )
        return f"{label}\n" + "\n".join(rows)

    return (
        f"<b>{PERIOD_LABELS[period]}</b>\n"
        f"{snapshot('Opening', period_values(game, period=period, snapshot='opening'))}"
        "\n\n"
        f"{snapshot('Latest', period_values(game, period=period, snapshot='latest'))}"
    )


def game_detail(
    game: Mapping[str, Any],
    *,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    tip = parse_timestamp(str(game["commence_time_utc"])).astimezone(ET)
    away = html.escape(str(game.get("away_team") or "Away"))
    home = html.escape(str(game.get("home_team") or "Home"))
    text = "\n\n".join(
        [
            f"🏀 <b>{away} @ {home}</b>",
            f"{tip:%A, %B %-d at %-I:%M %p ET}",
            f"Book: {html.escape(str(game.get('bookmaker') or 'nodata'))}",
            _period_lines(game, period="game"),
            _period_lines(game, period="first_half"),
            "Choose a period:",
        ]
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Full game", callback_data=callback_data("p", "game")
                ),
                InlineKeyboardButton(
                    "First half",
                    callback_data=callback_data("p", "first_half"),
                ),
            ],
            [
                InlineKeyboardButton(
                    "View lean history",
                    callback_data=callback_data("hist"),
                )
            ],
            [
                InlineKeyboardButton(
                    "← Back to games",
                    callback_data=callback_data("games", str(page)),
                )
            ],
        ]
    )


def period_summary(
    game: Mapping[str, Any],
    *,
    period: str,
) -> tuple[str, InlineKeyboardMarkup]:
    markets = MARKETS_BY_PERIOD[period]
    rows = [
        [
            InlineKeyboardButton(
                market.title(),
                callback_data=callback_data("m", market),
            )
            for market in markets
        ],
        [
            InlineKeyboardButton(
                "← Back to periods",
                callback_data=callback_data("back", "game"),
            )
        ],
    ]
    text = "\n\n".join(
        [
            (
                f"🏀 <b>{html.escape(str(game.get('away_team') or 'Away'))} @ "
                f"{html.escape(str(game.get('home_team') or 'Home'))}</b>"
            ),
            _period_lines(game, period=period),
            "Choose a market:",
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def side_label(game: Mapping[str, Any], market: str, side: str) -> str:
    if market == "total":
        return side.title()
    return str(game.get(f"{side}_team") or side.title())


def selected_market_context(
    game: Mapping[str, Any],
    *,
    period: str,
    market: str,
    side: str,
) -> dict[str, Any]:
    opening = period_values(game, period=period, snapshot="opening")
    latest = period_values(game, period=period, snapshot="latest")

    def selected(values: Mapping[str, Any]) -> tuple[Any, Any]:
        if market == "spread":
            return values[f"{side}_spread"], values[f"{side}_spread_price"]
        if market == "moneyline":
            return NO_DATA, values[f"{side}_moneyline"]
        return values["total"], values[f"{side}_price"]

    opening_line, opening_price = selected(opening)
    latest_line, latest_price = selected(latest)
    return {
        "opening_line": opening_line,
        "opening_price": opening_price,
        "latest_line": latest_line,
        "latest_price": latest_price,
    }


def selection_price_text(
    market: str,
    label: str,
    line: Any,
    price: Any,
) -> str:
    if market == "moneyline":
        return f"{label} {_signed(price)}"
    if market == "total":
        return f"{label} {_line(line)} ({_signed(price)})"
    return f"{label} {_signed(line)} ({_signed(price)})"


def market_summary(
    game: Mapping[str, Any],
    *,
    period: str,
    market: str,
) -> tuple[str, InlineKeyboardMarkup]:
    opening = period_values(game, period=period, snapshot="opening")
    latest = period_values(game, period=period, snapshot="latest")
    away = str(game.get("away_team") or "Away")
    home = str(game.get("home_team") or "Home")

    def values(snapshot: Mapping[str, Any]) -> str:
        if market == "spread":
            return (
                f"{html.escape(away)} {_signed(snapshot['away_spread'])} "
                f"({_signed(snapshot['away_spread_price'])}) · "
                f"{html.escape(home)} {_signed(snapshot['home_spread'])} "
                f"({_signed(snapshot['home_spread_price'])})"
            )
        if market == "moneyline":
            return (
                f"{html.escape(away)} {_signed(snapshot['away_moneyline'])} · "
                f"{html.escape(home)} {_signed(snapshot['home_moneyline'])}"
            )
        return (
            f"Over {_line(snapshot['total'])} "
            f"({_signed(snapshot['over_price'])}) · "
            f"Under {_line(snapshot['total'])} "
            f"({_signed(snapshot['under_price'])})"
        )

    sides = (
        (("over", "Over"), ("under", "Under"))
        if market == "total"
        else (("away", away), ("home", home))
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=callback_data("s", side),
                )
                for side, label in sides
            ],
            [
                InlineKeyboardButton(
                    "← Back to markets",
                    callback_data=callback_data("back", "markets"),
                )
            ],
        ]
    )
    text = "\n\n".join(
        [
            f"🏀 <b>{html.escape(away)} @ {html.escape(home)}</b>",
            f"<b>{PERIOD_LABELS[period]} · {market.title()}</b>",
            f"Opening\n{values(opening)}\n\nLatest\n{values(latest)}",
            "Choose a side:",
        ]
    )
    return text, keyboard


def game_history_text(
    history: Mapping[str, Any],
    *,
    include_revisions: bool,
) -> str:
    game = history["game"]
    lines = [
        (
            f"🏀 <b>{html.escape(str(game.get('away_team') or 'Away'))} @ "
            f"{html.escape(str(game.get('home_team') or 'Home'))}</b>"
        )
    ]
    active = history.get("active_revision")
    if active:
        lines.extend(
            [
                "",
                "<b>Latest active Claude lean</b>",
                (
                    "Full game: "
                    f"{html.escape(str(active.get('full_game_side_selection') or ''))} "
                    f"({html.escape(str(active.get('full_game_side_strength') or ''))}); "
                    f"{html.escape(str(active.get('full_game_total_selection') or ''))} "
                    f"({html.escape(str(active.get('full_game_total_strength') or ''))})"
                ),
                html.escape(str(active.get("summary") or "")),
            ]
        )
    else:
        lines.extend(["", "No published active Claude lean."])
    lines.extend(["", "<b>User thought / lean history</b>"])
    thoughts = history.get("thoughts") or []
    if not thoughts:
        lines.append("No user thoughts.")
    for thought in thoughts[-20:]:
        exact = str(thought.get("thought_text") or "")
        lines.append(
            (
                f"{html.escape(str(thought.get('submitted_at_et') or ''))} · "
                f"{html.escape(str(thought.get('period') or ''))} "
                f"{html.escape(str(thought.get('market') or ''))} "
                f"{html.escape(str(thought.get('side') or ''))}\n"
                f"{html.escape(exact[:500])}"
            )
        )
    if include_revisions:
        lines.extend(["", "<b>Superseded / deleted revisions</b>"])
        revisions = [
            revision
            for revision in history.get("revision_history") or []
            if revision.get("effective_status") != "active"
        ]
        if not revisions:
            lines.append("No superseded or deleted revisions.")
        for revision in revisions[-20:]:
            lines.append(
                (
                    f"{html.escape(str(revision.get('requested_at_et') or ''))} · "
                    f"{html.escape(str(revision.get('operation') or ''))} · "
                    f"{html.escape(str(revision.get('effective_status') or ''))}\n"
                    f"{html.escape(str(revision.get('summary') or '')[:500])}"
                )
            )
    rendered = "\n".join(lines)
    return rendered[:3900]


def selection_metadata(
    game: Mapping[str, Any],
    *,
    period: str,
    market: str,
    side: str,
) -> dict[str, Any]:
    context = selected_market_context(
        game,
        period=period,
        market=market,
        side=side,
    )
    return {
        "period": period,
        "market": market,
        "side": side_label(game, market, side),
        "opening_selected_line": context["opening_line"],
        "opening_selected_price": context["opening_price"],
        "latest_selected_line": context["latest_line"],
        "latest_selected_price": context["latest_price"],
    }


def selection_summary(
    game: Mapping[str, Any],
    *,
    period: str,
    market: str,
    side: str,
) -> str:
    context = selected_market_context(
        game,
        period=period,
        market=market,
        side=side,
    )
    label = side_label(game, market, side)
    return (
        f"{PERIOD_LABELS[period]} · {market.title()} · {label}\n"
        f"Opening: {selection_price_text(market, label, context['opening_line'], context['opening_price'])}\n"
        f"Latest: {selection_price_text(market, label, context['latest_line'], context['latest_price'])}"
    )


class WnbaGuesserBot:
    def __init__(
        self,
        *,
        config: GuesserConfig,
        store: SheetsStore,
    ) -> None:
        self.config = config
        self.store = store
        self.states: dict[int, dict[str, Any]] = {}
        self._allowed_ids: set[int] = set()
        self._allowlist_expires_at = 0.0

    async def post_init(self, application: Application) -> None:
        identity = await application.bot.get_me()
        if identity.username != self.config.expected_username:
            raise RuntimeError(
                "WNBA Guesser token does not belong to @wnbaguesser_bot"
            )
        self._allowed_ids = await self._sheet_call(
            self.store.allowed_user_ids
        )
        if not self._allowed_ids:
            raise RuntimeError(
                "wnba_allowed_users must be seeded before bot startup"
            )
        self._allowlist_expires_at = (
            time.monotonic() + ALLOWLIST_CACHE_SECONDS
        )
        await application.bot.set_my_commands(
            [
                BotCommand("wnba", "Browse WNBA games and add a lean"),
                BotCommand("wnba_thoughts", "Show recent WNBA leans"),
                BotCommand("wnba_cancel", "Cancel the active WNBA flow"),
            ]
        )
        log.info("WNBA Guesser running as @%s", identity.username)

    async def _sheet_call(self, function: Callable[..., Any], *args, **kwargs):
        return await asyncio.wait_for(
            asyncio.to_thread(function, *args, **kwargs),
            timeout=self.config.sheet_timeout_seconds,
        )

    async def _is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        if now >= self._allowlist_expires_at:
            self._allowed_ids = await self._sheet_call(
                self.store.allowed_user_ids
            )
            self._allowlist_expires_at = now + ALLOWLIST_CACHE_SECONDS
        return user_id in self._allowed_ids

    def authorized(self, handler: Handler) -> Handler:
        async def wrapped(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
        ) -> None:
            user = update.effective_user
            chat = update.effective_chat
            if user is None or chat is None or chat.type != ChatType.PRIVATE:
                return
            try:
                allowed = await self._is_allowed(user.id)
            except Exception:
                log.exception("Could not load WNBA Guesser allowlist")
                if update.callback_query:
                    await update.callback_query.answer(
                        "Allowlist check failed. Try again.", alert=True
                    )
                elif update.message:
                    await update.message.reply_text(
                        "❌ WNBA allowlist check failed. Try again."
                    )
                return
            if not allowed:
                if update.callback_query:
                    await update.callback_query.answer(
                        "Not authorized.", alert=True
                    )
                elif update.message:
                    await update.message.reply_text("Not authorized.")
                return
            await handler(update, context)

        return wrapped

    def _active_state(self, user_id: int) -> dict[str, Any] | None:
        state = self.states.get(user_id)
        if state is None:
            return None
        if datetime.now(UTC) >= state["expires_at"]:
            self.states.pop(user_id, None)
            return None
        return state

    async def _games(self) -> list[dict[str, Any]]:
        return await self._sheet_call(self.store.read_games)

    async def _fresh_game(self, event_id: str) -> dict[str, Any] | None:
        records = await self._games()
        return next(
            (
                game
                for game in records
                if selection_id(game) == event_id
            ),
            None,
        )

    async def start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await update.message.reply_text(
            "🏀 WNBA Guesser\n"
            "Choose a game, period, market, and side, then reply with your "
            "exact reasoning. Every update is appended; earlier leans are "
            "never overwritten."
        )
        await self.show_games(update, context)

    async def show_games(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        self.states.pop(update.effective_user.id, None)
        try:
            records = await self._games()
        except Exception:
            log.exception("Could not read WNBA games")
            await update.message.reply_text(
                "❌ Could not load WNBA games. Try again."
            )
            return
        text, keyboard = game_browser(
            records,
            page=0,
            now=datetime.now(UTC),
        )
        await update.message.reply_text(text, reply_markup=keyboard)

    async def recent_thoughts(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        try:
            thoughts = await self._sheet_call(
                self.store.read_recent_thoughts,
                limit=10,
            )
        except Exception:
            log.exception("Could not read recent WNBA thoughts")
            await update.message.reply_text(
                "❌ Could not load recent WNBA thoughts. Try again."
            )
            return
        if not thoughts:
            await update.message.reply_text("No WNBA thoughts found.")
            return
        chunks = ["Recent WNBA thoughts:"]
        for thought in thoughts:
            exact = str(thought.get("thought_text") or "")
            displayed = exact[:800]
            if len(exact) > len(displayed):
                displayed += "\n[truncated for Telegram display]"
            chunks.append(
                "\n".join(
                    [
                        str(thought.get("submitted_at_et") or "Unknown time"),
                        (
                            f"{thought.get('away_team', '')} @ "
                            f"{thought.get('home_team', '')}"
                        ),
                        (
                            f"{PERIOD_LABELS.get(str(thought.get('period')), thought.get('period', ''))}"
                            f" · {str(thought.get('market') or '').title()}"
                            f" · {thought.get('side', '')}"
                        ).strip(" ·"),
                        displayed,
                    ]
                )
            )
        text = "\n\n".join(chunks)
        while text:
            split_at = min(3900, len(text))
            if split_at < len(text):
                newline = text.rfind("\n", 0, split_at)
                if newline > 1000:
                    split_at = newline
            await update.message.reply_text(text[:split_at])
            text = text[split_at:].lstrip("\n")

    async def cancel(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        cancelled = self.states.pop(update.effective_user.id, None) is not None
        await update.message.reply_text(
            "WNBA guess cancelled." if cancelled else "No active WNBA guess."
        )

    async def callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        data = str(query.data or "")
        user_id = update.effective_user.id
        try:
            if data.startswith("games:"):
                page = int(data.split(":", 1)[1])
                self.states.pop(user_id, None)
                records = await self._games()
                text, keyboard = game_browser(
                    records,
                    page=page,
                    now=datetime.now(UTC),
                )
                await query.edit_message_text(
                    text,
                    reply_markup=keyboard,
                )
                await query.answer()
                return
            if data.startswith("g:"):
                _, page_raw, event_id = data.split(":", 2)
                if not EVENT_ID_PATTERN.fullmatch(event_id):
                    raise ValueError("invalid game callback")
                game = await self._fresh_game(event_id)
                if game is None:
                    await query.answer(
                        "Game no longer available.", alert=True
                    )
                    return
                self.states[user_id] = {
                    "event_id": event_id,
                    "game": game,
                    "page": int(page_raw),
                    "expires_at": datetime.now(UTC) + SELECTION_TTL,
                    "completed_message_ids": set(),
                }
                text, keyboard = game_detail(game, page=0)
                await query.edit_message_text(
                    text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
                await query.answer()
                return

            state = self._active_state(user_id)
            if state is None:
                await query.answer(
                    "This WNBA selection expired. Run /wnba again.",
                    alert=True,
                )
                return
            state["expires_at"] = datetime.now(UTC) + SELECTION_TTL
            if data in {"hist", "revs"}:
                history = await self._sheet_call(
                    self.store.read_game_history,
                    event_id=state["event_id"],
                )
                text = game_history_text(
                    history,
                    include_revisions=data == "revs",
                )
                rows = []
                if data == "hist":
                    rows.append(
                        [
                            InlineKeyboardButton(
                                "Superseded / deleted",
                                callback_data=callback_data("revs"),
                            )
                        ]
                    )
                rows.append(
                    [
                        InlineKeyboardButton(
                            "← Back to game",
                            callback_data=callback_data("back", "game"),
                        )
                    ]
                )
                keyboard = InlineKeyboardMarkup(rows)
            elif data == "back:game":
                state.pop("period", None)
                state.pop("market", None)
                state.pop("side", None)
                state.pop("prompt_message_id", None)
                text, keyboard = game_detail(
                    state["game"], page=state["page"]
                )
            elif data == "back:markets":
                state.pop("market", None)
                state.pop("side", None)
                state.pop("prompt_message_id", None)
                text, keyboard = period_summary(
                    state["game"], period=state["period"]
                )
            elif data.startswith("p:"):
                period = data.split(":", 1)[1]
                if period not in PERIOD_LABELS:
                    raise ValueError("invalid period")
                state["period"] = period
                state.pop("market", None)
                state.pop("side", None)
                text, keyboard = period_summary(
                    state["game"], period=period
                )
            elif data.startswith("m:"):
                market = data.split(":", 1)[1]
                if (
                    "period" not in state
                    or market not in MARKETS_BY_PERIOD[state["period"]]
                ):
                    raise ValueError("invalid market")
                state["market"] = market
                state.pop("side", None)
                text, keyboard = market_summary(
                    state["game"],
                    period=state["period"],
                    market=market,
                )
            elif data.startswith("s:"):
                side = data.split(":", 1)[1]
                market = state.get("market")
                valid = (
                    {"over", "under"}
                    if market == "total"
                    else {"away", "home"}
                )
                if market is None or side not in valid:
                    raise ValueError("invalid side")
                state["side"] = side
                await self._prompt_reasoning(query, state)
                return
            elif data == "repeat":
                if not all(
                    key in state for key in ("period", "market", "side")
                ):
                    raise ValueError("selection is incomplete")
                fresh = await self._fresh_game(state["event_id"])
                if fresh is None:
                    await query.answer(
                        "Game no longer available.", alert=True
                    )
                    return
                state["game"] = fresh
                await self._prompt_reasoning(query, state)
                return
            else:
                raise ValueError("unsupported callback")
            await query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            await query.answer()
        except (TimeoutError, ValueError):
            log.exception("Invalid or timed-out WNBA callback: %s", data)
            await query.answer("Could not complete that action.", alert=True)
        except Exception:
            log.exception("WNBA callback failed: %s", data)
            await query.answer("WNBA Sheet request failed.", alert=True)

    async def _prompt_reasoning(
        self,
        query,
        state: dict[str, Any],
    ) -> None:
        state.pop("attempt_message_id", None)
        state.pop("attempt_chat_id", None)
        state.pop("attempt_text", None)
        summary = selection_summary(
            state["game"],
            period=state["period"],
            market=state["market"],
            side=state["side"],
        )
        await query.edit_message_text(
            f"<blockquote>{html.escape(summary)}</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "← Back to markets",
                            callback_data=callback_data("back", "markets"),
                        )
                    ]
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        prompt = await query.message.reply_text(
            "Enter your exact lean/reasoning and any line or price where your "
            "preference changes:",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Type your WNBA reasoning",
            ),
        )
        state["prompt_message_id"] = prompt.message_id
        await query.answer()

    async def capture_reasoning(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.message
        state = self._active_state(update.effective_user.id)
        if state is None:
            await message.reply_text(
                "Run /wnba to start a WNBA guess."
            )
            return
        if message.message_id in state["completed_message_ids"]:
            await message.reply_text("✅ That WNBA update was already saved.")
            return
        reply = message.reply_to_message
        if (
            reply is None
            or reply.message_id != state.get("prompt_message_id")
        ):
            await message.reply_text(
                "Reply to the active WNBA reasoning prompt, or use "
                "/wnba_cancel."
            )
            return
        exact_text = message.text
        if not exact_text.strip():
            await message.reply_text("WNBA reasoning cannot be empty.")
            return
        attempt_message_id = state.get("attempt_message_id")
        if attempt_message_id is not None:
            if exact_text != state["attempt_text"]:
                await message.reply_text(
                    "A previous WNBA update is awaiting an idempotent retry. "
                    "Reply with that exact text, or /wnba_cancel."
                )
                return
            submission_message_id = int(attempt_message_id)
            submission_chat_id = int(state["attempt_chat_id"])
            stored_text = state["attempt_text"]
        else:
            submission_message_id = message.message_id
            submission_chat_id = update.effective_chat.id
            stored_text = exact_text
            state["attempt_message_id"] = submission_message_id
            state["attempt_chat_id"] = submission_chat_id
            state["attempt_text"] = stored_text

        try:
            game = await self._fresh_game(state["event_id"])
            if game is None:
                await message.reply_text(
                    "❌ The selected game is no longer available."
                )
                return
            state["game"] = game
            created, record = await self._sheet_call(
                self.store.append_thought_record,
                thought_id=(
                    f"telegram:{submission_chat_id}:"
                    f"{submission_message_id}"
                ),
                source="telegram",
                event_id=state["event_id"],
                thought_text=stored_text,
                now=datetime.now(UTC),
                telegram_metadata={
                    "telegram_user_id": update.effective_user.id,
                    "telegram_username": (
                        update.effective_user.username or ""
                    ),
                    "telegram_chat_id": submission_chat_id,
                    "telegram_message_id": submission_message_id,
                },
                selection_metadata=selection_metadata(
                    game,
                    period=state["period"],
                    market=state["market"],
                    side=state["side"],
                ),
            )
        except Exception:
            log.exception("Could not append WNBA reasoning")
            await message.reply_text(
                "❌ WNBA Sheet write was not confirmed. Reply with the exact "
                "same text to retry the original submission, or /wnba_cancel."
            )
            return

        state["completed_message_ids"].update(
            {submission_message_id, message.message_id}
        )
        state.pop("attempt_message_id", None)
        state.pop("attempt_chat_id", None)
        state.pop("attempt_text", None)
        state.pop("prompt_message_id", None)
        state["expires_at"] = datetime.now(UTC) + SELECTION_TTL
        status = "saved" if created else "already saved"
        summary = selection_summary(
            game,
            period=state["period"],
            market=state["market"],
            side=state["side"],
        )
        await message.reply_text(
            f"✅ WNBA update {status}.\n"
            f"{game.get('away_team', '')} @ {game.get('home_team', '')}\n"
            f"{summary.splitlines()[0]}\n"
            f"{record.get('submitted_at_et', '')}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Add another update",
                            callback_data=callback_data("repeat"),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Choose another game",
                            callback_data=callback_data("games", "0"),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "View game history",
                            callback_data=callback_data("hist"),
                        )
                    ],
                ]
            ),
        )


def build_application(
    config: GuesserConfig,
    store: SheetsStore,
) -> Application:
    bot = WnbaGuesserBot(config=config, store=store)
    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(bot.post_init)
        .build()
    )
    for command, handler in (
        ("start", bot.start),
        ("wnba", bot.show_games),
        ("wnba_thoughts", bot.recent_thoughts),
        ("wnba_cancel", bot.cancel),
    ):
        application.add_handler(
            CommandHandler(command, bot.authorized(handler))
        )
    application.add_handler(
        CallbackQueryHandler(
            bot.authorized(bot.callback),
            pattern=(
                r"^(?:games:\d+|g:\d+:[A-Za-z0-9_-]{1,48}|"
                r"p:(?:game|first_half)|m:(?:spread|moneyline|total)|"
                r"s:(?:away|home|over|under)|"
                r"back:(?:game|markets)|repeat|hist|revs)$"
            ),
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            bot.authorized(bot.capture_reasoning),
        )
    )
    return application


def main() -> None:
    core = Config.from_env(require_google=True, require_odds=False)
    config = GuesserConfig.from_env()
    store = SheetsStore.connect(
        sheet_id=core.sheet_id,
        credentials_b64=core.google_credentials_b64,
        service_account_json=core.google_service_account_json,
    )
    build_application(config, store).run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
