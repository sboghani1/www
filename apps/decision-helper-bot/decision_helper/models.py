from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


SPORTS = {"nfl", "nba"}
PICK_TYPES = {"winner", "spread", "total"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def normalize_team(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def team_matches(left: str, right: str) -> bool:
    if normalize_team(left) == normalize_team(right):
        return True
    left_words = re.findall(r"[a-z0-9]+", left.casefold())
    right_words = re.findall(r"[a-z0-9]+", right.casefold())
    return bool(left_words and right_words and left_words[-1] == right_words[-1])


@dataclass(frozen=True)
class MarketQuote:
    point: float | None
    price: int | None
    bookmaker_count: int


@dataclass(frozen=True)
class MarketEvent:
    id: str
    sport: str
    commence_time: str
    away_team: str
    home_team: str
    markets: dict[str, MarketQuote]

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.id,
            "sport": self.sport,
            "commence_time": self.commence_time,
            "away_team": self.away_team,
            "home_team": self.home_team,
            "consensus": {
                key: asdict(value) for key, value in self.markets.items()
            },
        }


@dataclass(frozen=True)
class ExtractedPick:
    sport: str
    event_id: str
    pick_type: str
    selection: str
    line: float | None
    confidence_probability: float | None
    confidence_label: str
    reasoning: str
    uncertainties: tuple[str, ...]


@dataclass(frozen=True)
class Extraction:
    picks: tuple[ExtractedPick, ...]
    immediate_feedback: str


@dataclass(frozen=True)
class FinalGame:
    id: str
    sport: str
    commence_time: str
    away_team: str
    home_team: str
    away_score: int
    home_score: int


@dataclass(frozen=True)
class DecisionReview:
    process_score: int
    luck_summary: str
    what_worked: str
    what_to_improve: str
    bias_tags: tuple[str, ...]


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric or null")
    return float(value)


def extraction_from_payload(
    payload: dict[str, Any],
    events: list[MarketEvent],
) -> Extraction:
    raw_picks = payload.get("picks")
    if not isinstance(raw_picks, list):
        raise ValueError("picks must be an array")
    by_id = {event.id: event for event in events}
    picks: list[ExtractedPick] = []
    for index, raw in enumerate(raw_picks):
        if not isinstance(raw, dict):
            raise ValueError(f"picks[{index}] must be an object")
        sport = _text(raw.get("sport"), f"picks[{index}].sport").casefold()
        event_id = _text(raw.get("event_id"), f"picks[{index}].event_id")
        pick_type = _text(
            raw.get("pick_type"), f"picks[{index}].pick_type"
        ).casefold()
        selection = _text(
            raw.get("selection"), f"picks[{index}].selection"
        )
        if sport not in SPORTS:
            raise ValueError(f"picks[{index}].sport is unsupported")
        if pick_type not in PICK_TYPES:
            raise ValueError(f"picks[{index}].pick_type is unsupported")
        event = by_id.get(event_id)
        if event is None or event.sport != sport:
            raise ValueError(f"picks[{index}].event_id is not a candidate")
        if pick_type in {"winner", "spread"}:
            if selection not in {event.away_team, event.home_team}:
                raise ValueError(
                    f"picks[{index}].selection must match a candidate team"
                )
        else:
            normalized_selection = selection.casefold()
            if normalized_selection not in {"over", "under"}:
                raise ValueError(
                    f"picks[{index}].selection must be Over or Under"
                )
            selection = normalized_selection.title()
        line = _number(raw.get("line"), f"picks[{index}].line")
        if pick_type == "winner" and line is not None:
            raise ValueError(f"picks[{index}].line must be null for winner")
        confidence = _number(
            raw.get("confidence_probability"),
            f"picks[{index}].confidence_probability",
        )
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"picks[{index}].confidence_probability must be 0-1"
            )
        uncertainties = raw.get("uncertainties", [])
        if not isinstance(uncertainties, list):
            raise ValueError(f"picks[{index}].uncertainties must be an array")
        picks.append(
            ExtractedPick(
                sport=sport,
                event_id=event_id,
                pick_type=pick_type,
                selection=selection,
                line=line,
                confidence_probability=confidence,
                confidence_label=_text(
                    raw.get("confidence_label", ""),
                    f"picks[{index}].confidence_label",
                    required=False,
                ),
                reasoning=_text(
                    raw.get("reasoning", ""),
                    f"picks[{index}].reasoning",
                    required=False,
                ),
                uncertainties=tuple(
                    _text(
                        item,
                        f"picks[{index}].uncertainties",
                    )
                    for item in uncertainties
                ),
            )
        )
    return Extraction(
        picks=tuple(picks),
        immediate_feedback=_text(
            payload.get("immediate_feedback", ""),
            "immediate_feedback",
            required=False,
        ),
    )


def review_from_payload(payload: dict[str, Any]) -> DecisionReview:
    score = payload.get("process_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise ValueError("process_score must be an integer from 1 to 5")
    raw_tags = payload.get("bias_tags", [])
    if not isinstance(raw_tags, list):
        raise ValueError("bias_tags must be an array")
    return DecisionReview(
        process_score=score,
        luck_summary=_text(payload.get("luck_summary", ""), "luck_summary"),
        what_worked=_text(payload.get("what_worked", ""), "what_worked"),
        what_to_improve=_text(
            payload.get("what_to_improve", ""), "what_to_improve"
        ),
        bias_tags=tuple(_text(tag, "bias_tags") for tag in raw_tags),
    )


def json_array(values: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def grade_pick(
    *,
    pick_type: str,
    selection: str,
    line: float | None,
    away_team: str,
    home_team: str,
    away_score: int,
    home_score: int,
) -> str:
    if pick_type == "winner":
        if away_score == home_score:
            return "push"
        winner = away_team if away_score > home_score else home_team
        return "win" if selection == winner else "loss"
    if line is None:
        return "ungradeable"
    if pick_type == "spread":
        if selection == away_team:
            margin = away_score - home_score + line
        elif selection == home_team:
            margin = home_score - away_score + line
        else:
            return "ungradeable"
    elif pick_type == "total":
        margin = away_score + home_score - line
        if selection == "Under":
            margin = -margin
    else:
        return "ungradeable"
    if margin > 0:
        return "win"
    if margin < 0:
        return "loss"
    return "push"


def implied_probability(price: int | None) -> float | None:
    if price is None or price == 0:
        return None
    if price > 0:
        return 100 / (price + 100)
    return -price / (-price + 100)


def closing_value(
    *,
    pick_type: str,
    selection: str,
    pick_line: float | None,
    closing_line: float | None,
    pick_price: int | None,
    closing_price: int | None,
) -> float | None:
    if pick_type == "winner":
        opened = implied_probability(pick_price)
        closed = implied_probability(closing_price)
        if opened is None or closed is None:
            return None
        return closed - opened
    if pick_line is None or closing_line is None:
        return None
    if pick_type == "spread":
        return pick_line - closing_line
    if pick_type == "total":
        return (
            closing_line - pick_line
            if selection == "Over"
            else pick_line - closing_line
        )
    return None
