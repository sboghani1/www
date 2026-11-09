"""Tracked YouTube pickers' NBA sides next to your bait.

The ``nba-channel-intake`` timer (nfl-audio-parser) parses the Doc's Sports
daily NBA show into the forwarder's opinion store and exports Craig Trapp's
and Indian Cowboy's latest pregame full-game sides and totals, plus the dates
each picker's shows cover, to ``picker-picks.json`` in Mel's state directory.
Mel only reads that file. For a baited game each picker is on the same side,
the opposite side, has no side pick (the show covered the date but not this
side), or isn't in yet (no parsed show for that date).
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import parse_timestamp


log = logging.getLogger(__name__)

PICKERS = ("Craig Trapp", "Indian Cowboy")
SHORT_NAMES = {"Craig Trapp": "Trapp", "Indian Cowboy": "Cowboy"}
SAME, OPPOSITE, NO_PICK, NOT_IN = "same", "opposite", "no_pick", "not_in"


def espn_id(event_id: str) -> str:
    """Mel's ``espn-401909955`` is the opinion store's ``401909955``."""
    return event_id.removeprefix("espn-")


@dataclass(frozen=True)
class PickerView:
    picker: str
    relation: str
    side: dict[str, Any] | None = None
    total: dict[str, Any] | None = None


class PickerFeed:
    """The exported picks file, reloaded when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._data = None, {}
            return self._data
        if mtime != self._mtime:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                self._mtime = mtime
            except (OSError, json.JSONDecodeError) as error:
                log.warning("Picker picks unreadable: %s", error)
        return self._data

    def views(
        self, event_id: str, commence_time: str, selection: str, eastern
    ) -> list[PickerView]:
        data = self.load()
        game_day = parse_timestamp(commence_time).astimezone(eastern).date().isoformat()
        key = espn_id(event_id)
        views = []
        for picker in PICKERS:
            covered = any(
                row.get("picker_name") == picker
                and row.get("valid_from_date", "") <= game_day <= row.get("valid_through_date", "")
                for row in data.get("coverage") or []
            )
            picks = [
                row
                for row in data.get("picks") or []
                if row.get("picker_name") == picker and str(row.get("event_id")) == key
            ]
            side = next((row for row in picks if row.get("market_family") == "game_side"), None)
            total = next((row for row in picks if row.get("market_family") == "game_total"), None)
            if side is not None:
                relation = SAME if side.get("direction") == selection else OPPOSITE
            elif covered or total is not None:
                relation = NO_PICK
            else:
                relation = NOT_IN
            views.append(PickerView(picker, relation, side, total))
        return views


def describe_pick(row: dict[str, Any]) -> str:
    """'Grizzlies +3.5', 'Lakers ML', 'Under 222.5', plus any condition."""
    direction = str(row.get("direction") or "")
    line = row.get("line")
    if row.get("market_family") == "game_total":
        text = direction if line is None else f"{direction} {line:g}"
    else:
        short = direction.split()[-1] if direction else direction
        if row.get("contract_type") == "moneyline":
            text = f"{short} ML"
        elif line is None:
            text = short
        else:
            text = f"{short} {line:+g}" if line else f"{short} pk"
    if row.get("opinion_status") == "conditional" and row.get("condition_text"):
        text += f" ({str(row['condition_text']).rstrip('.')})"
    return text


def format_views(views: list[PickerView]) -> str:
    """One line per picker for a bait message."""
    icons = {SAME: "🤝", OPPOSITE: "⚔️", NO_PICK: "➖", NOT_IN: "⏳"}
    lines = ["👥 Trapp & Cowboy (Doc's Sports):"]
    for view in views:
        name = SHORT_NAMES[view.picker]
        if view.relation == SAME:
            text = f"{describe_pick(view.side)}, same side as you"
        elif view.relation == OPPOSITE:
            text = f"{describe_pick(view.side)}, opposite side"
        elif view.relation == NO_PICK:
            text = "no side pick"
            if view.total is not None:
                text += f" (total: {describe_pick(view.total)})"
        else:
            text = "that day's show isn't parsed yet"
        lines.append(f"{icons[view.relation]} {name}: {text}")
    return "\n".join(lines)


def signature(views: list[PickerView]) -> str:
    """What changed for a follow-up: relations and the picks behind them."""
    return json.dumps(
        [
            [
                view.picker,
                view.relation,
                describe_pick(view.side) if view.side else None,
                describe_pick(view.total) if view.total else None,
            ]
            for view in views
        ]
    )


def picker_stats(
    baits: list[dict[str, Any]], feed: PickerFeed, eastern
) -> dict[str, Counter]:
    """Your final position per game (its last bait row, when that is a
    take), graded, split by where each picker stood."""
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bait in sorted(baits, key=lambda row: row["ordinal"]):
        by_game[bait["event_id"]].append(bait)
    groups: dict[str, Counter] = defaultdict(Counter)
    labels = {
        SAME: "same side as you",
        OPPOSITE: "opposite side",
        NO_PICK: "no side pick",
        NOT_IN: "show not parsed",
    }
    for rows in by_game.values():
        final = rows[-1]
        if (
            final["action"] != "take"
            or final["status"] != "final"
            or final["result"] not in {"win", "loss", "push"}
        ):
            continue
        views = feed.views(final["event_id"], final["commence_time"], final["selection"], eastern)
        for view in views:
            groups[f"{SHORT_NAMES[view.picker]} {labels[view.relation]}"][final["result"]] += 1
        sided = [view.relation for view in views if view.relation in {SAME, OPPOSITE}]
        if len(sided) == 2:
            if sided == [SAME, SAME]:
                both = "both with you"
            elif sided == [OPPOSITE, OPPOSITE]:
                both = "both against you"
            else:
                both = "split"
            groups[both][final["result"]] += 1
    return groups
