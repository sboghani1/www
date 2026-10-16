from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from www_sqlite import Migration, apply_migrations, connect, transaction

from .opinion_schema import OPINION_MIGRATION

SCHEMA_VERSION = "nfl_audio_parser_ingest_v1"
REVIEW_SCHEMA_VERSION = "nfl_audio_picks_v1"
BET_TYPES = {
    "moneyline",
    "other",
    "prop",
    "spread",
    "team_total",
    "total",
}
BET_STRUCTURES = {"straight", "parlay", "teaser"}
PERIODS = {"game", "1h", "2h", "1q", "2q", "3q", "4q"}
FINAL_STATUSES = {"active", "retracted"}
CONVICTION_LABELS = {
    0: "no_pick",
    1: "speculative",
    2: "lean",
    3: "standard",
    4: "strong",
    5: "best_bet",
}
PROHIBITED_KEYS = {
    "audio",
    "audio_bytes",
    "audio_path",
    "filename",
    "raw_provider_response",
    "source_filename",
    "source_path",
    "source_url",
    "spotify_url",
    "url",
    "video_title",
    "youtube_url",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE parser_runs (
            run_id TEXT PRIMARY KEY,
            segment_sha256 TEXT NOT NULL,
            source_start_seconds REAL NOT NULL,
            source_end_seconds REAL NOT NULL,
            season INTEGER NOT NULL,
            week INTEGER NOT NULL,
            transcription_provider TEXT NOT NULL,
            provider_transcript_id TEXT,
            transcript_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(segment_sha256, season, week)
        )
        """,
        """
        CREATE TABLE transcript_utterances (
            run_id TEXT NOT NULL REFERENCES parser_runs(run_id) ON DELETE CASCADE,
            utterance_index INTEGER NOT NULL,
            speaker_label TEXT NOT NULL,
            source_start_seconds REAL NOT NULL,
            source_end_seconds REAL NOT NULL,
            confidence REAL,
            exact_text TEXT NOT NULL,
            words_json TEXT NOT NULL,
            PRIMARY KEY(run_id, utterance_index)
        )
        """,
        """
        CREATE TABLE extraction_revisions (
            revision_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES parser_runs(run_id) ON DELETE CASCADE,
            content_sha256 TEXT NOT NULL,
            schedule_json TEXT NOT NULL,
            review_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(run_id, content_sha256)
        )
        """,
        """
        CREATE TABLE speaker_mappings (
            revision_id TEXT NOT NULL
                REFERENCES extraction_revisions(revision_id) ON DELETE CASCADE,
            speaker_label TEXT NOT NULL,
            proposed_name TEXT,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            PRIMARY KEY(revision_id, speaker_label)
        )
        """,
        """
        CREATE TABLE parsed_picks (
            revision_id TEXT NOT NULL
                REFERENCES extraction_revisions(revision_id) ON DELETE CASCADE,
            pick_index INTEGER NOT NULL,
            event_id TEXT,
            speaker_label TEXT NOT NULL,
            speaker_name TEXT,
            bet_structure TEXT NOT NULL,
            group_id TEXT,
            leg_index INTEGER,
            teaser_points REAL,
            bet_type TEXT NOT NULL,
            period TEXT NOT NULL,
            selection TEXT NOT NULL,
            line REAL,
            price INTEGER,
            conviction_score INTEGER NOT NULL,
            conviction_label TEXT NOT NULL,
            final_status TEXT NOT NULL,
            extraction_confidence REAL,
            normalized_pick_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            review_flags_json TEXT NOT NULL,
            PRIMARY KEY(revision_id, pick_index)
        )
        """,
        """
        CREATE TABLE review_decisions (
            decision_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL
                REFERENCES extraction_revisions(revision_id) ON DELETE CASCADE,
            reviewed_at_utc TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
            notes TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX parser_runs_season_week
            ON parser_runs(season, week, created_at_utc)
        """,
        """
        CREATE INDEX extraction_revisions_run_created
            ON extraction_revisions(run_id, created_at_utc)
        """,
        """
        CREATE INDEX parsed_picks_event
            ON parsed_picks(event_id, final_status)
        """,
    )
    for statement in statements:
        connection.execute(statement)


MIGRATIONS = (
    Migration(1, "create-audio-parser-store", _create_schema),
    OPINION_MIGRATION,
)


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
    return value


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _reject_prohibited_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in PROHIBITED_KEYS:
                raise ValueError(f"{path}.{key} is prohibited")
            _reject_prohibited_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_prohibited_keys(nested, f"{path}[{index}]")


def _validate_sha256(value: Any, field: str) -> str:
    text = _require_text(value, field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return text


def _validate_transcript(
    transcript: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    utterances = _require_list(transcript.get("utterances"), "transcript.utterances")
    if not utterances:
        raise ValueError("transcript.utterances must not be empty")
    normalized: list[dict[str, Any]] = []
    by_index: dict[int, dict[str, Any]] = {}
    for position, value in enumerate(utterances):
        utterance = _require_dict(value, f"transcript.utterances[{position}]")
        index = utterance.get("utterance_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"transcript.utterances[{position}].utterance_index is invalid")
        if index in by_index:
            raise ValueError(f"Duplicate utterance_index: {index}")
        speaker = _require_text(
            utterance.get("speaker_label"),
            f"transcript.utterances[{position}].speaker_label",
        )
        start = _require_number(
            utterance.get("source_start_seconds"),
            f"transcript.utterances[{position}].source_start_seconds",
        )
        end = _require_number(
            utterance.get("source_end_seconds"),
            f"transcript.utterances[{position}].source_end_seconds",
        )
        if start < 0 or end <= start:
            raise ValueError(f"transcript.utterances[{position}] has invalid timestamps")
        text = _require_text(
            utterance.get("text"), f"transcript.utterances[{position}].text"
        )
        word_values = _require_list(
            utterance.get("words", []),
            f"transcript.utterances[{position}].words",
        )
        words: list[dict[str, Any]] = []
        for word_position, word_value in enumerate(word_values):
            word = _require_dict(
                word_value,
                f"transcript.utterances[{position}].words[{word_position}]",
            )
            word_start = _require_number(
                word.get("source_start_seconds"),
                f"transcript.utterances[{position}].words[{word_position}]"
                ".source_start_seconds",
            )
            word_end = _require_number(
                word.get("source_end_seconds"),
                f"transcript.utterances[{position}].words[{word_position}]"
                ".source_end_seconds",
            )
            if word_start < start - 0.001 or word_end > end + 0.001 or word_end < word_start:
                raise ValueError(
                    f"transcript.utterances[{position}].words[{word_position}] "
                    "timestamps exceed the utterance"
                )
            word_confidence = word.get("confidence")
            if word_confidence is not None:
                word_confidence = _require_number(
                    word_confidence,
                    f"transcript.utterances[{position}].words[{word_position}]"
                    ".confidence",
                )
                if not 0 <= word_confidence <= 1:
                    raise ValueError(
                        f"transcript.utterances[{position}].words[{word_position}] "
                        "confidence is outside 0..1"
                    )
            words.append(
                {
                    "text": _require_text(
                        word.get("text"),
                        f"transcript.utterances[{position}].words[{word_position}].text",
                    ),
                    "source_start_seconds": word_start,
                    "source_end_seconds": word_end,
                    "confidence": word_confidence,
                }
            )
        confidence = utterance.get("confidence")
        if confidence is not None:
            confidence = _require_number(
                confidence, f"transcript.utterances[{position}].confidence"
            )
            if not 0 <= confidence <= 1:
                raise ValueError(
                    f"transcript.utterances[{position}].confidence is outside 0..1"
                )
        row = {
            "utterance_index": index,
            "speaker_label": speaker,
            "source_start_seconds": start,
            "source_end_seconds": end,
            "confidence": confidence,
            "text": text,
            "words": words,
        }
        normalized.append(row)
        by_index[index] = row
    normalized.sort(key=lambda row: row["utterance_index"])
    return normalized, by_index


def _validate_evidence(
    segments: Any,
    utterances: dict[int, dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    values = _require_list(segments, field)
    normalized: list[dict[str, Any]] = []
    for position, value in enumerate(values):
        segment = _require_dict(value, f"{field}[{position}]")
        index = segment.get("utterance_index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"{field}[{position}].utterance_index is invalid")
        utterance = utterances.get(index)
        if utterance is None:
            raise ValueError(f"{field}[{position}] references unknown utterance {index}")
        speaker = _require_text(
            segment.get("speaker_label"), f"{field}[{position}].speaker_label"
        )
        if speaker != utterance["speaker_label"]:
            raise ValueError(f"{field}[{position}] speaker does not match transcript")
        exact_text = _require_text(
            segment.get("exact_text"), f"{field}[{position}].exact_text"
        )
        if exact_text not in utterance["text"]:
            raise ValueError(f"{field}[{position}] is not an exact transcript substring")
        start = _require_number(
            segment.get("source_start_seconds"),
            f"{field}[{position}].source_start_seconds",
        )
        end = _require_number(
            segment.get("source_end_seconds"),
            f"{field}[{position}].source_end_seconds",
        )
        if (
            start < utterance["source_start_seconds"] - 0.001
            or end > utterance["source_end_seconds"] + 0.001
            or end <= start
        ):
            raise ValueError(f"{field}[{position}] timestamps exceed the utterance")
        normalized.append(
            {
                "utterance_index": index,
                "speaker_label": speaker,
                "source_start_seconds": start,
                "source_end_seconds": end,
                "exact_text": exact_text,
            }
        )
    return normalized


def _validate_payload(payload: Any) -> dict[str, Any]:
    payload = _require_dict(payload, "payload")
    _reject_prohibited_keys(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
    transcript = _require_dict(payload.get("transcript"), "transcript")
    schedule = _require_dict(payload.get("schedule"), "schedule")
    review = _require_dict(payload.get("review"), "review")
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError(f"review.schema_version must be {REVIEW_SCHEMA_VERSION!r}")
    request = _require_dict(review.get("request"), "review.request")
    segment_sha256 = _validate_sha256(
        request.get("segment_sha256"), "review.request.segment_sha256"
    )
    start = _require_number(
        request.get("start_seconds"), "review.request.start_seconds"
    )
    end = _require_number(request.get("end_seconds"), "review.request.end_seconds")
    if start < 0 or end <= start:
        raise ValueError("review.request timestamps are invalid")
    season = request.get("season")
    week = request.get("week")
    if isinstance(season, bool) or not isinstance(season, int) or season < 2000:
        raise ValueError("review.request.season is invalid")
    if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 22:
        raise ValueError("review.request.week is invalid")

    provider = _require_text(transcript.get("provider"), "transcript.provider")
    provider_id = transcript.get("provider_transcript_id")
    if provider_id is not None and not isinstance(provider_id, str):
        raise ValueError("transcript.provider_transcript_id must be a string or null")
    utterances, utterances_by_index = _validate_transcript(transcript)
    for utterance in utterances:
        if (
            utterance["source_start_seconds"] < start - 0.001
            or utterance["source_end_seconds"] > end + 0.001
        ):
            raise ValueError("A transcript utterance is outside the requested segment")

    games = _require_list(schedule.get("games"), "schedule.games")
    if schedule.get("season") != season or schedule.get("requested_week") != week:
        raise ValueError("schedule season/week does not match review.request")
    event_ids: set[str] = set()
    for index, value in enumerate(games):
        game = _require_dict(value, f"schedule.games[{index}]")
        event_ids.add(
            _require_text(game.get("event_id"), f"schedule.games[{index}].event_id")
        )
        game_season = game.get("season")
        game_week = game.get("week")
        if game_season != season:
            raise ValueError(f"schedule.games[{index}].season is invalid")
        if (
            isinstance(game_week, bool)
            or not isinstance(game_week, int)
            or game_week not in {week - 1, week, week + 1}
        ):
            raise ValueError(f"schedule.games[{index}].week is outside context")

    speakers = _require_list(review.get("speakers", []), "review.speakers")
    normalized_speakers: list[dict[str, Any]] = []
    for index, value in enumerate(speakers):
        speaker = _require_dict(value, f"review.speakers[{index}]")
        label = _require_text(
            speaker.get("speaker_label"),
            f"review.speakers[{index}].speaker_label",
        )
        name = speaker.get("name")
        if name is not None:
            name = _require_text(name, f"review.speakers[{index}].name")
        confidence = _require_number(
            speaker.get("confidence"), f"review.speakers[{index}].confidence"
        )
        if not 0 <= confidence <= 1:
            raise ValueError(f"review.speakers[{index}].confidence is outside 0..1")
        evidence = _validate_evidence(
            speaker.get("evidence", []),
            utterances_by_index,
            f"review.speakers[{index}].evidence",
        )
        normalized_speakers.append(
            {
                "speaker_label": label,
                "name": name,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    picks = _require_list(review.get("picks"), "review.picks")
    normalized_picks: list[dict[str, Any]] = []
    seen_pick_indexes: set[int] = set()
    for position, value in enumerate(picks):
        pick = _require_dict(value, f"review.picks[{position}]")
        pick_index = pick.get("pick_index")
        if isinstance(pick_index, bool) or not isinstance(pick_index, int) or pick_index < 1:
            raise ValueError(f"review.picks[{position}].pick_index is invalid")
        if pick_index in seen_pick_indexes:
            raise ValueError(f"Duplicate pick_index: {pick_index}")
        seen_pick_indexes.add(pick_index)
        event_id = pick.get("event_id")
        if event_id is not None:
            event_id = _require_text(event_id, f"review.picks[{position}].event_id")
            if event_id not in event_ids:
                raise ValueError(
                    f"review.picks[{position}].event_id is absent from schedule"
                )
        evidence = _validate_evidence(
            pick.get("evidence_segments"),
            utterances_by_index,
            f"review.picks[{position}].evidence_segments",
        )
        if not evidence:
            raise ValueError(f"review.picks[{position}] has no evidence")
        rationale = _validate_evidence(
            pick.get("rationale_segments", []),
            utterances_by_index,
            f"review.picks[{position}].rationale_segments",
        )
        conviction = _require_dict(
            pick.get("conviction"), f"review.picks[{position}].conviction"
        )
        score = conviction.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
            raise ValueError(f"review.picks[{position}].conviction.score is invalid")
        label = _require_text(
            conviction.get("label"),
            f"review.picks[{position}].conviction.label",
        )
        if label != CONVICTION_LABELS[score]:
            raise ValueError(
                f"review.picks[{position}] conviction score/label do not match"
            )
        markers = _require_list(
            conviction.get("explicit_markers", []),
            f"review.picks[{position}].conviction.explicit_markers",
        )
        evidence_text = "\n".join(segment["exact_text"] for segment in evidence)
        for marker_index, marker_value in enumerate(markers):
            marker = _require_text(
                marker_value,
                f"review.picks[{position}].conviction.explicit_markers[{marker_index}]",
            )
            if marker not in evidence_text:
                raise ValueError(
                    f"review.picks[{position}] conviction marker is not in evidence"
                )
        extraction_confidence = pick.get("extraction_confidence")
        if extraction_confidence is not None:
            extraction_confidence = _require_number(
                extraction_confidence,
                f"review.picks[{position}].extraction_confidence",
            )
            if not 0 <= extraction_confidence <= 1:
                raise ValueError(
                    f"review.picks[{position}].extraction_confidence is outside 0..1"
                )
        normalized_pick = dict(pick)
        normalized_pick["evidence_segments"] = evidence
        normalized_pick["rationale_segments"] = rationale
        speaker_name = pick.get("speaker_name")
        if speaker_name is not None:
            speaker_name = _require_text(
                speaker_name, f"review.picks[{position}].speaker_name"
            )
        bet_type = _require_text(
            pick.get("bet_type"), f"review.picks[{position}].bet_type"
        )
        if bet_type not in BET_TYPES:
            raise ValueError(f"review.picks[{position}].bet_type is unsupported")
        bet_structure = _require_text(
            pick.get("bet_structure"),
            f"review.picks[{position}].bet_structure",
        )
        if bet_structure not in BET_STRUCTURES:
            raise ValueError(
                f"review.picks[{position}].bet_structure is unsupported"
            )
        group_id = pick.get("group_id")
        leg_index = pick.get("leg_index")
        teaser_points = pick.get("teaser_points")
        if bet_structure == "straight":
            if group_id is not None or leg_index is not None or teaser_points is not None:
                raise ValueError(
                    f"review.picks[{position}] straight bet has grouped-leg fields"
                )
        else:
            group_id = _require_text(
                group_id, f"review.picks[{position}].group_id"
            )
            if (
                isinstance(leg_index, bool)
                or not isinstance(leg_index, int)
                or leg_index < 1
            ):
                raise ValueError(f"review.picks[{position}].leg_index is invalid")
            if bet_structure == "teaser" and teaser_points is not None:
                teaser_points = _require_number(
                    teaser_points,
                    f"review.picks[{position}].teaser_points",
                )
            elif bet_structure == "parlay" and teaser_points is not None:
                raise ValueError(
                    f"review.picks[{position}] parlay has teaser_points"
                )
        period = _require_text(
            pick.get("period"), f"review.picks[{position}].period"
        )
        if period not in PERIODS:
            raise ValueError(f"review.picks[{position}].period is unsupported")
        final_status = _require_text(
            pick.get("final_status"),
            f"review.picks[{position}].final_status",
        )
        if final_status not in FINAL_STATUSES:
            raise ValueError(f"review.picks[{position}].final_status is unsupported")
        line = pick.get("line")
        if line is not None:
            line = _require_number(line, f"review.picks[{position}].line")
        price = pick.get("price")
        if price is not None and (
            isinstance(price, bool) or not isinstance(price, int) or price == 0
        ):
            raise ValueError(f"review.picks[{position}].price is invalid")
        normalized_picks.append(
            {
                "pick": normalized_pick,
                "pick_index": pick_index,
                "event_id": event_id,
                "speaker_label": _require_text(
                    pick.get("speaker_label"),
                    f"review.picks[{position}].speaker_label",
                ),
                "speaker_name": speaker_name,
                "bet_structure": bet_structure,
                "group_id": group_id,
                "leg_index": leg_index,
                "teaser_points": teaser_points,
                "bet_type": bet_type,
                "period": period,
                "selection": _require_text(
                    pick.get("selection"), f"review.picks[{position}].selection"
                ),
                "line": line,
                "price": price,
                "conviction_score": score,
                "conviction_label": label,
                "final_status": final_status,
                "extraction_confidence": extraction_confidence,
                "evidence": evidence,
                "review_flags": _require_list(
                    pick.get("review_flags", []),
                    f"review.picks[{position}].review_flags",
                ),
            }
        )

    return {
        "segment_sha256": segment_sha256,
        "start": start,
        "end": end,
        "season": season,
        "week": week,
        "provider": provider,
        "provider_id": provider_id,
        "transcript": transcript,
        "utterances": utterances,
        "schedule": schedule,
        "review": review,
        "speakers": normalized_speakers,
        "picks": normalized_picks,
    }


class AudioParserStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)

    def ingest(self, payload: Any) -> dict[str, Any]:
        validated = _validate_payload(payload)
        transcript_sha256 = sha256_json(validated["transcript"])
        content_sha256 = sha256_json(
            {
                "schedule": validated["schedule"],
                "review": validated["review"],
            }
        )
        now = utc_now()
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)
            with transaction(connection, immediate=True):
                existing = connection.execute(
                    """
                    SELECT run_id, transcript_sha256
                    FROM parser_runs
                    WHERE segment_sha256=? AND season=? AND week=?
                    """,
                    (
                        validated["segment_sha256"],
                        validated["season"],
                        validated["week"],
                    ),
                ).fetchone()
                created_run = existing is None
                if existing is None:
                    run_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO parser_runs(
                            run_id, segment_sha256, source_start_seconds,
                            source_end_seconds, season, week,
                            transcription_provider, provider_transcript_id,
                            transcript_sha256, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            validated["segment_sha256"],
                            validated["start"],
                            validated["end"],
                            validated["season"],
                            validated["week"],
                            validated["provider"],
                            validated["provider_id"],
                            transcript_sha256,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO transcript_utterances(
                            run_id, utterance_index, speaker_label,
                            source_start_seconds, source_end_seconds,
                            confidence, exact_text, words_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                run_id,
                                utterance["utterance_index"],
                                utterance["speaker_label"],
                                utterance["source_start_seconds"],
                                utterance["source_end_seconds"],
                                utterance["confidence"],
                                utterance["text"],
                                canonical_json(utterance["words"]),
                            )
                            for utterance in validated["utterances"]
                        ],
                    )
                else:
                    run_id = str(existing["run_id"])
                    if existing["transcript_sha256"] != transcript_sha256:
                        raise ValueError(
                            "The same audio segment already has a different transcript"
                        )

                existing_revision = connection.execute(
                    """
                    SELECT revision_id
                    FROM extraction_revisions
                    WHERE run_id=? AND content_sha256=?
                    """,
                    (run_id, content_sha256),
                ).fetchone()
                if existing_revision is not None:
                    return {
                        "run_id": run_id,
                        "revision_id": str(existing_revision["revision_id"]),
                        "created_run": created_run,
                        "created_revision": False,
                    }

                revision_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO extraction_revisions(
                        revision_id, run_id, content_sha256, schedule_json,
                        review_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        run_id,
                        content_sha256,
                        canonical_json(validated["schedule"]),
                        canonical_json(validated["review"]),
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO speaker_mappings(
                        revision_id, speaker_label, proposed_name,
                        confidence, evidence_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            revision_id,
                            speaker["speaker_label"],
                            speaker["name"],
                            speaker["confidence"],
                            canonical_json(speaker["evidence"]),
                        )
                        for speaker in validated["speakers"]
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO parsed_picks(
                        revision_id, pick_index, event_id, speaker_label,
                        speaker_name, bet_structure, group_id, leg_index,
                        teaser_points, bet_type, period, selection, line, price,
                        conviction_score, conviction_label, final_status,
                        extraction_confidence, normalized_pick_json,
                        evidence_json, review_flags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            revision_id,
                            pick["pick_index"],
                            pick["event_id"],
                            pick["speaker_label"],
                            pick["speaker_name"],
                            pick["bet_structure"],
                            pick["group_id"],
                            pick["leg_index"],
                            pick["teaser_points"],
                            pick["bet_type"],
                            pick["period"],
                            pick["selection"],
                            pick["line"],
                            pick["price"],
                            pick["conviction_score"],
                            pick["conviction_label"],
                            pick["final_status"],
                            pick["extraction_confidence"],
                            canonical_json(pick["pick"]),
                            canonical_json(pick["evidence"]),
                            canonical_json(pick["review_flags"]),
                        )
                        for pick in validated["picks"]
                    ],
                )
                return {
                    "run_id": run_id,
                    "revision_id": revision_id,
                    "created_run": created_run,
                    "created_revision": True,
                }

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        with connect(self.path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, r.segment_sha256, r.source_start_seconds,
                       r.source_end_seconds, r.season, r.week,
                       r.transcription_provider, r.provider_transcript_id,
                       r.created_at_utc,
                       COUNT(DISTINCT e.revision_id) AS revision_count,
                       COUNT(DISTINCT p.pick_index || ':' || p.revision_id)
                           AS parsed_pick_count
                FROM parser_runs r
                LEFT JOIN extraction_revisions e ON e.run_id=r.run_id
                LEFT JOIN parsed_picks p ON p.revision_id=e.revision_id
                GROUP BY r.run_id
                ORDER BY r.created_at_utc DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(
        self, run_id: str, *, revision_id: str | None = None
    ) -> dict[str, Any]:
        with connect(self.path, read_only=True) as connection:
            run = connection.execute(
                "SELECT * FROM parser_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run_id: {run_id}")
            utterances = [
                {
                    **dict(row),
                    "words": json.loads(row["words_json"]),
                }
                for row in connection.execute(
                    """
                    SELECT utterance_index, speaker_label,
                           source_start_seconds, source_end_seconds,
                           confidence, exact_text, words_json
                    FROM transcript_utterances
                    WHERE run_id=?
                    ORDER BY utterance_index
                    """,
                    (run_id,),
                )
            ]
            if revision_id is None:
                revision = connection.execute(
                    """
                    SELECT * FROM extraction_revisions
                    WHERE run_id=?
                    ORDER BY created_at_utc DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
            else:
                revision = connection.execute(
                    """
                    SELECT * FROM extraction_revisions
                    WHERE run_id=? AND revision_id=?
                    """,
                    (run_id, revision_id),
                ).fetchone()
            result: dict[str, Any] = {
                "run": dict(run),
                "utterances": utterances,
                "revision": None,
            }
            if revision is not None:
                result["revision"] = {
                    **dict(revision),
                    "schedule": json.loads(revision["schedule_json"]),
                    "review": json.loads(revision["review_json"]),
                }
            return result

    def add_review_decision(
        self,
        revision_id: str,
        *,
        reviewer: str,
        decision: str,
        notes: str = "",
    ) -> str:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        reviewer = reviewer.strip()
        if not reviewer:
            raise ValueError("reviewer must not be empty")
        decision_id = str(uuid.uuid4())
        with connect(self.path) as connection:
            apply_migrations(connection, MIGRATIONS)
            with transaction(connection, immediate=True):
                exists = connection.execute(
                    """
                    SELECT 1 FROM extraction_revisions WHERE revision_id=?
                    """,
                    (revision_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(f"Unknown revision_id: {revision_id}")
                connection.execute(
                    """
                    INSERT INTO review_decisions(
                        decision_id, revision_id, reviewed_at_utc,
                        reviewer, decision, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        revision_id,
                        utc_now(),
                        reviewer,
                        decision,
                        notes,
                    ),
                )
        return decision_id
