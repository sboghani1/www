from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from www_sqlite import apply_migrations, connect, transaction

from .store import CONVICTION_LABELS, MIGRATIONS, canonical_json, sha256_json, utc_now

OPINION_SCHEMA_VERSION = "sports_opinions_v1"
SPORTS = {"nfl", "nba"}
MARKET_FAMILIES = {
    "game_side",
    "game_total",
    "team_total",
    "player_prop",
    "team_prop",
    "other",
}
CONTRACT_TYPES = {"spread", "moneyline", "total", "prop", "other"}
PERIODS = {"game", "1h", "2h", "1q", "2q", "3q", "4q"}
OPINION_STATUSES = {"active", "conditional", "pass", "retracted"}
OPINION_STAGES = {"early_look", "analysis", "betting_card", "contest", "final"}
SIDE_ROLES = {"favorite", "underdog", "pickem", "unknown", "not_applicable"}
BET_STRUCTURES = {"straight", "parlay", "teaser"}
PROHIBITED_CONTENT_KEYS = {
    "audio",
    "audio_bytes",
    "audio_path",
    "filename",
    "raw_provider_response",
    "source_filename",
    "source_path",
    "source_url",
    "spotify_url",
    "title",
    "url",
    "video_title",
    "youtube_url",
}
STAT_GROUPS = {
    "picker": "observation.picker_name",
    "sport": "observation.sport",
    "conviction_tier": """
        CASE
            WHEN observation.conviction_score >= 4 THEN 'strong'
            WHEN observation.conviction_score <= 2 THEN 'weak'
            ELSE 'standard'
        END
    """,
    "side_role": "observation.side_role",
    "market_family": "observation.market_family",
    "change_type": "observation.change_type",
    "opinion_stage": "observation.opinion_stage",
    "program": "COALESCE(program.display_name, 'Unknown')",
    "agreement_bucket": "observation.agreement_bucket",
    "agreement_count": "observation.agreement_count",
    "picker_count": "observation.picker_count",
}


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _require_number(value, field)


def _parse_date(value: Any, field: str) -> date:
    text = _require_text(value, field)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def _parse_timestamp(value: Any, field: str) -> datetime:
    text = _require_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _reject_prohibited_content(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in PROHIBITED_CONTENT_KEYS:
                raise ValueError(f"{path}.{key} is prohibited")
            _reject_prohibited_content(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_prohibited_content(nested, f"{path}[{index}]")


def source_reference(url: str) -> tuple[str, str]:
    parsed = urlparse(_require_text(url, "source_url"))
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    reference: str | None = None
    provider: str
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        provider = "youtube"
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            reference = values[0] if values else None
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"embed", "live", "shorts"}:
                reference = parts[1]
    elif host == "youtu.be":
        provider = "youtube"
        parts = [part for part in parsed.path.split("/") if part]
        reference = parts[0] if parts else None
    elif host == "open.spotify.com":
        provider = "spotify"
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"episode", "show"}:
            reference = f"{parts[0]}:{parts[1]}"
    else:
        raise ValueError("source_url must be a supported YouTube or Spotify URL")
    if not reference:
        raise ValueError("source_url does not contain a stable content identifier")
    digest = hashlib.sha256(f"{provider}:{reference}".encode()).hexdigest()
    return provider, digest


def _side_role(
    *,
    supplied: Any,
    contract_type: str,
    line: float | None,
) -> str:
    if supplied is not None:
        role = _require_text(supplied, "observation.side_role")
        if role not in SIDE_ROLES:
            raise ValueError("observation.side_role is unsupported")
        return role
    if contract_type == "spread" and line is not None:
        if line < 0:
            return "favorite"
        if line > 0:
            return "underdog"
        return "pickem"
    if contract_type in {"total", "prop"}:
        return "not_applicable"
    return "unknown"


def _get_or_create_named(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    display_name: str,
    now: str,
) -> str:
    normalized = _normalize_name(display_name)
    row = connection.execute(
        f"SELECT {id_column} FROM {table} WHERE normalized_name=?",
        (normalized,),
    ).fetchone()
    if row is not None:
        return str(row[id_column])
    identifier = str(uuid.uuid4())
    connection.execute(
        f"""
        INSERT INTO {table}(
            {id_column}, normalized_name, display_name, created_at_utc
        ) VALUES (?, ?, ?, ?)
        """,
        (identifier, normalized, display_name, now),
    )
    return identifier


def _grade_observation(
    observation: sqlite3.Row,
    *,
    away_score: int,
    home_score: int,
) -> tuple[str, dict[str, Any]]:
    details = {
        "away_team": observation["away_team"],
        "home_team": observation["home_team"],
        "away_score": away_score,
        "home_score": home_score,
        "selection": observation["selection"],
        "direction": observation["direction"],
        "line": observation["line"],
        "period": observation["period"],
    }
    if observation["opinion_status"] not in {"active", "conditional"}:
        return "ungradeable", {**details, "reason": "inactive opinion"}
    if observation["period"] != "game":
        return "ungradeable", {**details, "reason": "period score unavailable"}
    contract_type = str(observation["contract_type"])
    direction = observation["direction"]
    line = observation["line"]
    away_team = str(observation["away_team"])
    home_team = str(observation["home_team"])
    if contract_type == "spread":
        if direction not in {away_team, home_team} or line is None:
            return "ungradeable", {**details, "reason": "missing side or line"}
        selected_margin = (
            away_score - home_score
            if direction == away_team
            else home_score - away_score
        )
        margin = selected_margin + float(line)
    elif contract_type == "moneyline":
        if direction not in {away_team, home_team}:
            return "ungradeable", {**details, "reason": "missing team direction"}
        selected_score, other_score = (
            (away_score, home_score)
            if direction == away_team
            else (home_score, away_score)
        )
        margin = selected_score - other_score
    elif contract_type == "total":
        if direction not in {"Over", "Under"} or line is None:
            return "ungradeable", {**details, "reason": "missing total or line"}
        actual = away_score + home_score
        margin = actual - float(line)
        if direction == "Under":
            margin = -margin
        details["actual_total"] = actual
    else:
        return "ungradeable", {
            **details,
            "reason": f"{contract_type} requires a separate result feed",
        }
    if margin > 0:
        return "right", details
    if margin < 0:
        return "wrong", details
    return "push", details


class OpinionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)

    def ingest(self, payload: Any, *, source_url: str) -> dict[str, Any]:
        root = _require_dict(payload, "payload")
        _reject_prohibited_content(root)
        if root.get("schema_version") != OPINION_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {OPINION_SCHEMA_VERSION}"
            )
        content = _require_dict(root.get("content"), "content")
        provider, reference_hash = source_reference(source_url)
        supplied_provider = content.get("source_provider")
        if supplied_provider is not None and supplied_provider != provider:
            raise ValueError("content.source_provider does not match source_url")
        sport = _require_text(content.get("sport"), "content.sport").casefold()
        if sport not in SPORTS:
            raise ValueError("content.sport must be nfl or nba")
        valid_from = _parse_date(
            content.get("valid_from_date"), "content.valid_from_date"
        )
        valid_through = _parse_date(
            content.get("valid_through_date"), "content.valid_through_date"
        )
        if valid_through < valid_from:
            raise ValueError("content.valid_through_date precedes valid_from_date")
        timezone_name = str(content.get("valid_timezone") or "America/New_York")
        try:
            valid_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("content.valid_timezone is unknown") from error
        published_at = _parse_timestamp(
            content.get("published_at_utc"), "content.published_at_utc"
        )
        start = _require_number(
            content.get("source_start_seconds"), "content.source_start_seconds"
        )
        end = _require_number(
            content.get("source_end_seconds"), "content.source_end_seconds"
        )
        if start < 0 or end <= start:
            raise ValueError("content source range is invalid")
        program_name = _optional_text(content.get("program_name"), "program_name")
        label = _optional_text(content.get("window_label"), "window_label")
        segment_sha256 = _optional_text(
            content.get("segment_sha256"), "content.segment_sha256"
        )
        extraction_revision_id = _optional_text(
            content.get("extraction_revision_id"),
            "content.extraction_revision_id",
        )

        events = _require_list(root.get("events"), "events")
        normalized_events: dict[str, dict[str, Any]] = {}
        for position, value in enumerate(events):
            event = _require_dict(value, f"events[{position}]")
            event_id = _require_text(
                event.get("event_id"), f"events[{position}].event_id"
            )
            event_sport = _require_text(
                event.get("sport"), f"events[{position}].sport"
            ).casefold()
            if event_sport != sport:
                raise ValueError(f"events[{position}].sport does not match content")
            commence = _parse_timestamp(
                event.get("commence_time_utc"),
                f"events[{position}].commence_time_utc",
            )
            local_date = commence.astimezone(valid_timezone).date()
            if not valid_from <= local_date <= valid_through:
                raise ValueError(
                    f"events[{position}] is outside the prediction window"
                )
            normalized_events[event_id] = {
                "event_id": event_id,
                "sport": sport,
                "commence_time_utc": _timestamp(commence),
                "away_team": _require_text(
                    event.get("away_team"), f"events[{position}].away_team"
                ),
                "home_team": _require_text(
                    event.get("home_team"), f"events[{position}].home_team"
                ),
                "status": _require_text(
                    event.get("status", "scheduled"),
                    f"events[{position}].status",
                ),
                "away_score": event.get("away_score"),
                "home_score": event.get("home_score"),
            }
            for score_field in ("away_score", "home_score"):
                score = normalized_events[event_id][score_field]
                if score is not None and (
                    isinstance(score, bool)
                    or not isinstance(score, int)
                    or score < 0
                ):
                    raise ValueError(
                        f"events[{position}].{score_field} is invalid"
                    )
            if (
                normalized_events[event_id]["away_score"] is None
            ) != (
                normalized_events[event_id]["home_score"] is None
            ):
                raise ValueError(
                    f"events[{position}] must provide both final scores"
                )

        observations = _require_list(root.get("observations"), "observations")
        normalized_observations: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        for position, value in enumerate(observations):
            observation = _require_dict(value, f"observations[{position}]")
            observation_index = observation.get("observation_index")
            if (
                isinstance(observation_index, bool)
                or not isinstance(observation_index, int)
                or observation_index < 1
                or observation_index in seen_indexes
            ):
                raise ValueError(
                    f"observations[{position}].observation_index is invalid"
                )
            seen_indexes.add(observation_index)
            event_id = _require_text(
                observation.get("event_id"),
                f"observations[{position}].event_id",
            )
            if event_id not in normalized_events:
                raise ValueError(
                    f"observations[{position}].event_id is absent from events"
                )
            picker_name = _require_text(
                observation.get("picker_name"),
                f"observations[{position}].picker_name",
            )
            market_family = _require_text(
                observation.get("market_family"),
                f"observations[{position}].market_family",
            )
            if market_family not in MARKET_FAMILIES:
                raise ValueError(
                    f"observations[{position}].market_family is unsupported"
                )
            contract_type = _require_text(
                observation.get("contract_type"),
                f"observations[{position}].contract_type",
            )
            if contract_type not in CONTRACT_TYPES:
                raise ValueError(
                    f"observations[{position}].contract_type is unsupported"
                )
            period = _require_text(
                observation.get("period", "game"),
                f"observations[{position}].period",
            )
            if period not in PERIODS:
                raise ValueError(f"observations[{position}].period is unsupported")
            status = _require_text(
                observation.get("opinion_status", "active"),
                f"observations[{position}].opinion_status",
            )
            if status not in OPINION_STATUSES:
                raise ValueError(
                    f"observations[{position}].opinion_status is unsupported"
                )
            stage = _require_text(
                observation.get("opinion_stage", "analysis"),
                f"observations[{position}].opinion_stage",
            )
            if stage not in OPINION_STAGES:
                raise ValueError(
                    f"observations[{position}].opinion_stage is unsupported"
                )
            bet_structure = _require_text(
                observation.get("bet_structure", "straight"),
                f"observations[{position}].bet_structure",
            )
            if bet_structure not in BET_STRUCTURES:
                raise ValueError(
                    f"observations[{position}].bet_structure is unsupported"
                )
            source_seconds = _require_number(
                observation.get("source_seconds"),
                f"observations[{position}].source_seconds",
            )
            if not start <= source_seconds <= end:
                raise ValueError(
                    f"observations[{position}].source_seconds is outside segment"
                )
            line = _optional_number(
                observation.get("line"), f"observations[{position}].line"
            )
            price = observation.get("price")
            if price is not None and (
                isinstance(price, bool)
                or not isinstance(price, int)
                or price == 0
            ):
                raise ValueError(f"observations[{position}].price is invalid")
            conviction = _require_dict(
                observation.get("conviction"),
                f"observations[{position}].conviction",
            )
            score = conviction.get("score")
            if (
                isinstance(score, bool)
                or not isinstance(score, int)
                or score not in CONVICTION_LABELS
            ):
                raise ValueError(
                    f"observations[{position}].conviction.score is invalid"
                )
            conviction_label = _require_text(
                conviction.get("label"),
                f"observations[{position}].conviction.label",
            )
            if conviction_label != CONVICTION_LABELS[score]:
                raise ValueError(
                    f"observations[{position}] conviction score/label mismatch"
                )
            evidence = _require_list(
                observation.get("evidence_segments"),
                f"observations[{position}].evidence_segments",
            )
            if not evidence:
                raise ValueError(f"observations[{position}] has no evidence")
            normalized_evidence = []
            evidence_text: list[str] = []
            for evidence_index, evidence_value in enumerate(evidence):
                segment = _require_dict(
                    evidence_value,
                    f"observations[{position}].evidence_segments[{evidence_index}]",
                )
                exact_text = _require_text(
                    segment.get("exact_text"),
                    f"observations[{position}].evidence_segments"
                    f"[{evidence_index}].exact_text",
                )
                evidence_text.append(exact_text)
                normalized_evidence.append(dict(segment, exact_text=exact_text))
            markers = _require_list(
                conviction.get("explicit_markers", []),
                f"observations[{position}].conviction.explicit_markers",
            )
            for marker_index, marker_value in enumerate(markers):
                marker = _require_text(
                    marker_value,
                    f"observations[{position}].conviction.explicit_markers"
                    f"[{marker_index}]",
                )
                if not any(marker in text for text in evidence_text):
                    raise ValueError(
                        f"observations[{position}] conviction marker "
                        "is absent from evidence"
                    )
            declared_rank = observation.get("declared_rank")
            if declared_rank is not None and (
                isinstance(declared_rank, bool)
                or not isinstance(declared_rank, int)
                or declared_rank < 1
            ):
                raise ValueError(
                    f"observations[{position}].declared_rank is invalid"
                )
            extraction_confidence = _optional_number(
                observation.get("extraction_confidence"),
                f"observations[{position}].extraction_confidence",
            )
            if extraction_confidence is not None and not (
                0 <= extraction_confidence <= 1
            ):
                raise ValueError(
                    f"observations[{position}].extraction_confidence "
                    "is outside 0..1"
                )
            normalized_observations.append(
                {
                    "observation_index": observation_index,
                    "picker_name": picker_name,
                    "event_id": event_id,
                    "market_family": market_family,
                    "contract_type": contract_type,
                    "period": period,
                    "subject_key": str(observation.get("subject_key") or ""),
                    "prop_stat": str(observation.get("prop_stat") or ""),
                    "source_seconds": source_seconds,
                    "direction": _optional_text(
                        observation.get("direction"),
                        f"observations[{position}].direction",
                    ),
                    "line": line,
                    "price": price,
                    "side_role": _side_role(
                        supplied=observation.get("side_role"),
                        contract_type=contract_type,
                        line=line,
                    ),
                    "opinion_status": status,
                    "opinion_stage": stage,
                    "conviction_score": score,
                    "conviction_label": conviction_label,
                    "declared_units": _optional_number(
                        observation.get("declared_units"),
                        f"observations[{position}].declared_units",
                    ),
                    "declared_rank": declared_rank,
                    "hedged": _require_bool(
                        conviction.get("hedged", False),
                        f"observations[{position}].conviction.hedged",
                    ),
                    "conditional": _require_bool(
                        conviction.get("conditional", False),
                        f"observations[{position}].conviction.conditional",
                    ),
                    "condition_text": _optional_text(
                        conviction.get("condition_text"),
                        f"observations[{position}].conviction.condition_text",
                    ),
                    "bet_structure": bet_structure,
                    "group_id": _optional_text(
                        observation.get("group_id"),
                        f"observations[{position}].group_id",
                    ),
                    "leg_index": observation.get("leg_index"),
                    "selection": _require_text(
                        observation.get("selection"),
                        f"observations[{position}].selection",
                    ),
                    "evidence": normalized_evidence,
                    "explicit_markers": markers,
                    "extraction_confidence": extraction_confidence,
                    "review_flags": _require_list(
                        observation.get("review_flags", []),
                        f"observations[{position}].review_flags",
                    ),
                    "raw": observation,
                }
            )
            normalized = normalized_observations[-1]
            if (
                normalized["declared_units"] is not None
                and normalized["declared_units"] < 0
            ):
                raise ValueError(
                    f"observations[{position}].declared_units is invalid"
                )
            leg_index = normalized["leg_index"]
            if normalized["bet_structure"] == "straight":
                if normalized["group_id"] is not None or leg_index is not None:
                    raise ValueError(
                        f"observations[{position}] straight bet has grouped fields"
                    )
            else:
                if normalized["group_id"] is None:
                    raise ValueError(
                        f"observations[{position}].group_id is required"
                    )
                if (
                    isinstance(leg_index, bool)
                    or not isinstance(leg_index, int)
                    or leg_index < 1
                ):
                    raise ValueError(
                        f"observations[{position}].leg_index is invalid"
                    )

        payload_sha256 = sha256_json(root)
        now = utc_now()
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)
            with transaction(connection, immediate=True):
                window_row = connection.execute(
                    """
                    SELECT window_id FROM prediction_windows
                    WHERE sport=? AND valid_from_date=?
                      AND valid_through_date=? AND valid_timezone=?
                    """,
                    (
                        sport,
                        valid_from.isoformat(),
                        valid_through.isoformat(),
                        timezone_name,
                    ),
                ).fetchone()
                if window_row is None:
                    window_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO prediction_windows(
                            window_id, sport, valid_from_date,
                            valid_through_date, valid_timezone, label,
                            created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            window_id,
                            sport,
                            valid_from.isoformat(),
                            valid_through.isoformat(),
                            timezone_name,
                            label,
                            now,
                        ),
                    )
                else:
                    window_id = str(window_row["window_id"])

                program_id = None
                if program_name is not None:
                    program_id = _get_or_create_named(
                        connection,
                        table="programs",
                        id_column="program_id",
                        display_name=program_name,
                        now=now,
                    )
                content_row = connection.execute(
                    """
                    SELECT content_id, program_id, published_at_utc
                    FROM content_items
                    WHERE source_provider=? AND source_reference_hash=?
                    """,
                    (provider, reference_hash),
                ).fetchone()
                if content_row is None:
                    content_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO content_items(
                            content_id, source_provider,
                            source_reference_hash, program_id,
                            published_at_utc, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            content_id,
                            provider,
                            reference_hash,
                            program_id,
                            _timestamp(published_at),
                            now,
                        ),
                    )
                else:
                    content_id = str(content_row["content_id"])
                    if (
                        str(content_row["published_at_utc"])
                        != _timestamp(published_at)
                    ):
                        raise ValueError(
                            "source_url already has a different publication time"
                        )
                    existing_program = content_row["program_id"]
                    if (
                        existing_program is not None
                        and program_id is not None
                        and existing_program != program_id
                    ):
                        raise ValueError(
                            "source_url already belongs to a different program"
                        )
                    if existing_program is None and program_id is not None:
                        connection.execute(
                            "UPDATE content_items SET program_id=? WHERE content_id=?",
                            (program_id, content_id),
                        )

                for event in normalized_events.values():
                    existing_event = connection.execute(
                        "SELECT * FROM sporting_events WHERE event_id=?",
                        (event["event_id"],),
                    ).fetchone()
                    if existing_event is not None:
                        immutable = (
                            "sport",
                            "commence_time_utc",
                            "away_team",
                            "home_team",
                        )
                        if any(
                            str(existing_event[field]) != str(event[field])
                            for field in immutable
                        ):
                            raise ValueError(
                                f"event {event['event_id']} conflicts with stored data"
                            )
                        connection.execute(
                            """
                            UPDATE sporting_events
                            SET status=?, away_score=COALESCE(?, away_score),
                                home_score=COALESCE(?, home_score),
                                result_updated_at_utc=CASE
                                    WHEN ? IS NOT NULL AND ? IS NOT NULL
                                    THEN ? ELSE result_updated_at_utc END
                            WHERE event_id=?
                            """,
                            (
                                event["status"],
                                event["away_score"],
                                event["home_score"],
                                event["away_score"],
                                event["home_score"],
                                now,
                                event["event_id"],
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO sporting_events(
                                event_id, sport, commence_time_utc,
                                away_team, home_team, status,
                                away_score, home_score, result_updated_at_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event["event_id"],
                                event["sport"],
                                event["commence_time_utc"],
                                event["away_team"],
                                event["home_team"],
                                event["status"],
                                event["away_score"],
                                event["home_score"],
                                now
                                if event["away_score"] is not None
                                and event["home_score"] is not None
                                else None,
                            ),
                        )

                existing_revision = connection.execute(
                    """
                    SELECT content_revision_id
                    FROM content_revisions
                    WHERE content_id=? AND window_id=?
                      AND source_start_seconds=? AND source_end_seconds=?
                      AND payload_sha256=?
                    """,
                    (content_id, window_id, start, end, payload_sha256),
                ).fetchone()
                if existing_revision is not None:
                    return {
                        "content_id": content_id,
                        "content_revision_id": str(
                            existing_revision["content_revision_id"]
                        ),
                        "window_id": window_id,
                        "created_revision": False,
                        "observation_count": len(normalized_observations),
                    }
                if extraction_revision_id is not None:
                    extraction = connection.execute(
                        """
                        SELECT run_id FROM extraction_revisions
                        WHERE revision_id=?
                        """,
                        (extraction_revision_id,),
                    ).fetchone()
                    if extraction is None:
                        raise ValueError("content.extraction_revision_id is unknown")
                    utterances = {
                        int(row["utterance_index"]): str(row["exact_text"])
                        for row in connection.execute(
                            """
                            SELECT utterance_index, exact_text
                            FROM transcript_utterances
                            WHERE run_id=?
                            """,
                            (extraction["run_id"],),
                        )
                    }
                    for observation in normalized_observations:
                        for segment in observation["evidence"]:
                            utterance_index = segment.get("utterance_index")
                            if (
                                isinstance(utterance_index, bool)
                                or not isinstance(utterance_index, int)
                                or utterance_index not in utterances
                            ):
                                raise ValueError(
                                    "linked evidence has an invalid utterance_index"
                                )
                            if (
                                segment["exact_text"]
                                not in utterances[utterance_index]
                            ):
                                raise ValueError(
                                    "linked evidence is not an exact transcript substring"
                                )
                content_revision_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO content_revisions(
                        content_revision_id, content_id, window_id,
                        source_start_seconds, source_end_seconds,
                        segment_sha256, extraction_revision_id,
                        payload_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content_revision_id,
                        content_id,
                        window_id,
                        start,
                        end,
                        segment_sha256,
                        extraction_revision_id,
                        payload_sha256,
                        now,
                    ),
                )

                for observation in normalized_observations:
                    picker_id = _get_or_create_named(
                        connection,
                        table="pickers",
                        id_column="picker_id",
                        display_name=observation["picker_name"],
                        now=now,
                    )
                    thread_row = connection.execute(
                        """
                        SELECT thread_id FROM opinion_threads
                        WHERE picker_id=? AND event_id=?
                          AND market_family=? AND period=?
                          AND subject_key=? AND prop_stat=?
                        """,
                        (
                            picker_id,
                            observation["event_id"],
                            observation["market_family"],
                            observation["period"],
                            observation["subject_key"],
                            observation["prop_stat"],
                        ),
                    ).fetchone()
                    if thread_row is None:
                        thread_id = str(uuid.uuid4())
                        connection.execute(
                            """
                            INSERT INTO opinion_threads(
                                thread_id, picker_id, event_id,
                                market_family, period, subject_key,
                                prop_stat, created_at_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                thread_id,
                                picker_id,
                                observation["event_id"],
                                observation["market_family"],
                                observation["period"],
                                observation["subject_key"],
                                observation["prop_stat"],
                                now,
                            ),
                        )
                    else:
                        thread_id = str(thread_row["thread_id"])
                    connection.execute(
                        """
                        INSERT INTO opinion_observations(
                            observation_id, content_revision_id,
                            observation_index, thread_id, source_seconds,
                            contract_type, direction, line, price, side_role,
                            opinion_status, opinion_stage, conviction_score,
                            conviction_label, declared_units, declared_rank,
                            hedged, conditional, condition_text,
                            bet_structure, group_id, leg_index, selection,
                            evidence_json, explicit_markers_json,
                            extraction_confidence, review_flags_json,
                            normalized_observation_json, created_at_utc
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            content_revision_id,
                            observation["observation_index"],
                            thread_id,
                            observation["source_seconds"],
                            observation["contract_type"],
                            observation["direction"],
                            observation["line"],
                            observation["price"],
                            observation["side_role"],
                            observation["opinion_status"],
                            observation["opinion_stage"],
                            observation["conviction_score"],
                            observation["conviction_label"],
                            observation["declared_units"],
                            observation["declared_rank"],
                            int(observation["hedged"]),
                            int(observation["conditional"]),
                            observation["condition_text"],
                            observation["bet_structure"],
                            observation["group_id"],
                            observation["leg_index"],
                            observation["selection"],
                            canonical_json(observation["evidence"]),
                            canonical_json(observation["explicit_markers"]),
                            observation["extraction_confidence"],
                            canonical_json(observation["review_flags"]),
                            canonical_json(observation["raw"]),
                            now,
                        ),
                    )
        return {
            "content_id": content_id,
            "content_revision_id": content_revision_id,
            "window_id": window_id,
            "created_revision": True,
            "observation_count": len(normalized_observations),
        }

    def source(self, source_url: str) -> dict[str, Any]:
        provider, reference_hash = source_reference(source_url)
        with connect(self.path, read_only=True) as connection:
            content = connection.execute(
                """
                SELECT content.content_id, content.source_provider,
                       content.published_at_utc,
                       program.display_name AS program_name
                FROM content_items content
                LEFT JOIN programs program
                  ON program.program_id=content.program_id
                WHERE content.source_provider=?
                  AND content.source_reference_hash=?
                """,
                (provider, reference_hash),
            ).fetchone()
            if content is None:
                raise KeyError("No analyzed content matches source_url")
            observations = [
                {
                    **dict(row),
                    "evidence": json.loads(row["evidence_json"]),
                    "review_flags": json.loads(row["review_flags_json"]),
                }
                for row in connection.execute(
                    """
                    SELECT strength.observation_id,
                           strength.observation_index,
                           strength.picker_name, strength.sport,
                           strength.valid_from_date,
                           strength.valid_through_date,
                           strength.event_id, strength.away_team,
                           strength.home_team, strength.commence_time_utc,
                           strength.market_family, strength.period,
                           strength.selection, strength.direction,
                           strength.contract_type, strength.line,
                           strength.price, strength.side_role,
                           strength.opinion_status, strength.opinion_stage,
                           strength.conviction_score,
                           strength.conviction_label,
                           strength.picker_window_strength_rank,
                           strength.picker_window_observation_count,
                           strength.content_strength_rank,
                           strength.content_observation_count,
                           timeline.change_type, strength.source_seconds,
                           strength.evidence_json,
                           strength.review_flags_json
                    FROM opinion_strength_context strength
                    JOIN opinion_timeline timeline
                      ON timeline.observation_id=strength.observation_id
                    WHERE strength.content_id=?
                    ORDER BY strength.valid_from_date,
                             strength.commence_time_utc,
                             strength.source_seconds,
                             strength.observation_index
                    """,
                    (content["content_id"],),
                )
            ]
            games_by_id: dict[str, dict[str, Any]] = {}
            for observation in observations:
                event_id = str(observation["event_id"])
                game = games_by_id.setdefault(
                    event_id,
                    {
                        "event_id": event_id,
                        "sport": observation["sport"],
                        "away_team": observation["away_team"],
                        "home_team": observation["home_team"],
                        "commence_time_utc": observation["commence_time_utc"],
                        "opinion_count": 0,
                        "pickers": [],
                    },
                )
                game["opinion_count"] += 1
                picker_name = observation["picker_name"]
                if picker_name not in game["pickers"]:
                    game["pickers"].append(picker_name)
        return {
            "content": dict(content),
            "games": list(games_by_id.values()),
            "observations": observations,
        }

    def timeline(
        self,
        *,
        picker_name: str,
        event_id: str,
        market_family: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [
            "picker.normalized_name=?",
            "timeline.event_id=?",
        ]
        values: list[Any] = [_normalize_name(picker_name), event_id]
        if market_family is not None:
            if market_family not in MARKET_FAMILIES:
                raise ValueError("market_family is unsupported")
            conditions.append("timeline.market_family=?")
            values.append(market_family)
        with connect(self.path, read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT timeline.*, program.display_name AS program_name
                FROM opinion_timeline timeline
                JOIN pickers picker ON picker.picker_id=timeline.picker_id
                JOIN content_items content
                  ON content.content_id=timeline.content_id
                LEFT JOIN programs program
                  ON program.program_id=content.program_id
                WHERE {' AND '.join(conditions)}
                ORDER BY timeline.published_at_utc,
                         timeline.source_seconds,
                         timeline.observation_index
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_result(
        self,
        *,
        event_id: str,
        away_score: int,
        home_score: int,
    ) -> dict[str, Any]:
        if away_score < 0 or home_score < 0:
            raise ValueError("scores must be non-negative")
        now = utc_now()
        counts = {"right": 0, "wrong": 0, "push": 0, "ungradeable": 0}
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)
            with transaction(connection, immediate=True):
                event = connection.execute(
                    "SELECT event_id FROM sporting_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if event is None:
                    raise KeyError(f"Unknown event_id: {event_id}")
                connection.execute(
                    """
                    UPDATE sporting_events
                    SET status='final', away_score=?, home_score=?,
                        result_updated_at_utc=?
                    WHERE event_id=?
                    """,
                    (away_score, home_score, now, event_id),
                )
                rows = connection.execute(
                    """
                    SELECT * FROM current_opinion_observations
                    WHERE event_id=?
                    """,
                    (event_id,),
                ).fetchall()
                for observation in rows:
                    result, details = _grade_observation(
                        observation,
                        away_score=away_score,
                        home_score=home_score,
                    )
                    counts[result] += 1
                    connection.execute(
                        """
                        INSERT INTO opinion_grades(
                            observation_id, grading_method, result,
                            graded_at_utc, details_json
                        ) VALUES (?, 'stated_contract', ?, ?, ?)
                        ON CONFLICT(observation_id, grading_method)
                        DO UPDATE SET result=excluded.result,
                                      graded_at_utc=excluded.graded_at_utc,
                                      details_json=excluded.details_json
                        """,
                        (
                            observation["observation_id"],
                            result,
                            now,
                            canonical_json(details),
                        ),
                    )
        return {"event_id": event_id, "graded": sum(counts.values()), **counts}

    def stats(
        self,
        *,
        group_by: list[str],
        policy: str = "latest_pregame",
        sport: str | None = None,
    ) -> list[dict[str, Any]]:
        if not group_by:
            raise ValueError("group_by must contain at least one dimension")
        invalid = [value for value in group_by if value not in STAT_GROUPS]
        if invalid:
            raise ValueError(f"Unsupported stats dimensions: {', '.join(invalid)}")
        if policy not in {"latest_pregame", "all_observations"}:
            raise ValueError("policy must be latest_pregame or all_observations")
        source = (
            "latest_pregame_consensus_context"
            if policy == "latest_pregame"
            else "opinion_timeline"
        )
        agreement_dimensions = {
            "agreement_bucket",
            "agreement_count",
            "picker_count",
        }
        if policy == "all_observations" and agreement_dimensions.intersection(
            group_by
        ):
            raise ValueError(
                "agreement dimensions require the latest_pregame policy"
            )
        select_groups = [
            f"{STAT_GROUPS[value]} AS {value}" for value in group_by
        ]
        group_expressions = [STAT_GROUPS[value] for value in group_by]
        where = ["grade.grading_method='stated_contract'"]
        values: list[Any] = []
        if sport is not None:
            normalized_sport = sport.casefold()
            if normalized_sport not in SPORTS:
                raise ValueError("sport must be nfl or nba")
            where.append("observation.sport=?")
            values.append(normalized_sport)
        with connect(self.path, read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {', '.join(select_groups)},
                       COUNT(*) AS total,
                       SUM(grade.result='right') AS right,
                       SUM(grade.result='wrong') AS wrong,
                       SUM(grade.result='push') AS push,
                       SUM(grade.result='ungradeable') AS ungradeable,
                       CASE
                           WHEN SUM(grade.result IN ('right', 'wrong'))=0
                           THEN NULL
                           ELSE ROUND(
                               1.0 * SUM(grade.result='right')
                               / SUM(grade.result IN ('right', 'wrong')),
                               4
                           )
                       END AS win_rate
                FROM {source} observation
                JOIN opinion_grades grade
                  ON grade.observation_id=observation.observation_id
                JOIN content_items content
                  ON content.content_id=observation.content_id
                LEFT JOIN programs program
                  ON program.program_id=content.program_id
                WHERE {' AND '.join(where)}
                GROUP BY {', '.join(group_expressions)}
                ORDER BY {', '.join(group_expressions)}
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def consensus(
        self,
        *,
        event_id: str,
        market_family: str = "game_side",
        minimum_conviction: int = 0,
    ) -> list[dict[str, Any]]:
        if market_family not in MARKET_FAMILIES:
            raise ValueError("market_family is unsupported")
        if not 0 <= minimum_conviction <= 5:
            raise ValueError("minimum_conviction must be between 0 and 5")
        with connect(self.path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT direction, COUNT(*) AS picker_count,
                       ROUND(AVG(conviction_score), 3)
                           AS average_conviction,
                       GROUP_CONCAT(picker_name, ', ') AS pickers
                FROM latest_pregame_consensus_context
                WHERE event_id=? AND market_family=?
                  AND conviction_score>=?
                GROUP BY direction
                ORDER BY picker_count DESC, average_conviction DESC,
                         direction
                """,
                (event_id, market_family, minimum_conviction),
            ).fetchall()
        return [dict(row) for row in rows]
