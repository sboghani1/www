from __future__ import annotations

import json
import re
import sqlite3
import struct
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from www_sqlite import Migration, apply_migrations, connect, transaction

from .channel_registry import ChannelConfig


PIPELINE_STATUSES = {
    "discovered",
    "queued",
    "processing",
    "parsed",
    "skipped",
    "duplicate",
    "failed",
}
TERMINAL_STATUSES = {"parsed", "skipped", "duplicate"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _create_pipeline_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE pipeline_channels (
            channel_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            channel_reference_hash TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            program_name TEXT NOT NULL,
            sport TEXT NOT NULL,
            season INTEGER NOT NULL,
            expected_speakers_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE pipeline_videos (
            source_reference_hash TEXT PRIMARY KEY,
            channel_key TEXT NOT NULL
                REFERENCES pipeline_channels(channel_key),
            season INTEGER,
            week INTEGER,
            published_at_utc TEXT,
            duration_seconds REAL,
            metadata_sha256 TEXT,
            status TEXT NOT NULL CHECK(status IN (
                'discovered', 'queued', 'processing', 'parsed',
                'skipped', 'duplicate', 'failed'
            )),
            candidate_reason TEXT,
            terminal_reason TEXT,
            duplicate_of_hash TEXT
                REFERENCES pipeline_videos(source_reference_hash),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT,
            lease_expires_at_utc TEXT,
            next_retry_at_utc TEXT,
            provider_transcript_id TEXT,
            segment_sha256 TEXT,
            parser_run_id TEXT,
            extraction_revision_id TEXT,
            opinion_content_id TEXT,
            review_receipt_json TEXT,
            opinion_receipt_json TEXT,
            last_error TEXT,
            first_discovered_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL,
            started_at_utc TEXT,
            completed_at_utc TEXT,
            updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE pipeline_fingerprints (
            source_reference_hash TEXT PRIMARY KEY
                REFERENCES pipeline_videos(source_reference_hash)
                ON DELETE CASCADE,
            token_count INTEGER NOT NULL,
            shingle_size INTEGER NOT NULL,
            shingle_count INTEGER NOT NULL,
            compressed_hashes BLOB NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE pipeline_runs (
            pipeline_run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            channel_key TEXT,
            season INTEGER,
            weeks_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'running', 'succeeded', 'failed'
            )),
            discovered_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT
        )
        """,
        """
        CREATE INDEX pipeline_videos_ready
        ON pipeline_videos(
            status, next_retry_at_utc, published_at_utc, first_discovered_at_utc
        )
        """,
        """
        CREATE INDEX pipeline_videos_channel_week
        ON pipeline_videos(channel_key, season, week, status)
        """,
    )
    for statement in statements:
        connection.execute(statement)


PIPELINE_MIGRATIONS = (
    Migration(1, "create-channel-pipeline-ledger", _create_pipeline_schema),
)


@dataclass(frozen=True)
class DiscoveredVideo:
    source_reference_hash: str
    channel_key: str
    season: int | None
    week: int | None
    published_at_utc: str | None
    duration_seconds: float | None
    metadata_sha256: str
    candidate: bool
    reason: str


class PipelineStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def initialize(self) -> None:
        with connect(self.path) as connection:
            apply_migrations(connection, PIPELINE_MIGRATIONS)

    def sync_channel(self, channel: ChannelConfig) -> None:
        self.initialize()
        now = utc_now()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            connection.execute(
                """
                INSERT INTO pipeline_channels(
                    channel_key, provider, channel_reference_hash,
                    display_name, program_name, sport, season,
                    expected_speakers_json, config_sha256, active,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET
                    provider=excluded.provider,
                    channel_reference_hash=excluded.channel_reference_hash,
                    display_name=excluded.display_name,
                    program_name=excluded.program_name,
                    sport=excluded.sport,
                    season=excluded.season,
                    expected_speakers_json=excluded.expected_speakers_json,
                    config_sha256=excluded.config_sha256,
                    active=excluded.active,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    channel.key,
                    channel.provider,
                    channel.reference_hash,
                    channel.display_name,
                    channel.program_name,
                    channel.sport,
                    channel.season,
                    json.dumps(channel.expected_speakers),
                    channel.config_hash,
                    int(channel.active),
                    now,
                    now,
                ),
            )
        for seed in channel.seeds:
            self.seed_video(
                channel_key=channel.key,
                source_reference_hash=seed.source_reference_hash,
                season=seed.season,
                week=seed.week,
                status=seed.status,
                reason=seed.reason,
                duplicate_of_hash=seed.duplicate_of_hash,
            )

    def seed_video(
        self,
        *,
        channel_key: str,
        source_reference_hash: str,
        season: int,
        week: int,
        status: str,
        reason: str,
        duplicate_of_hash: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("Seed status must be terminal")
        if status == "duplicate" and duplicate_of_hash is None:
            raise ValueError("Duplicate seeds require duplicate lineage")
        if status != "duplicate" and duplicate_of_hash is not None:
            raise ValueError("Only duplicate seeds may declare lineage")
        now = utc_now()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            connection.execute(
                """
                INSERT OR IGNORE INTO pipeline_videos(
                    source_reference_hash, channel_key, season, week,
                    status, candidate_reason, terminal_reason,
                    duplicate_of_hash,
                    attempt_count, first_discovered_at_utc,
                    last_seen_at_utc, completed_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'seeded', ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    source_reference_hash,
                    channel_key,
                    season,
                    week,
                    status,
                    reason,
                    duplicate_of_hash,
                    now,
                    now,
                    now,
                    now,
                ),
            )

    def record_discovery(self, video: DiscoveredVideo) -> str:
        self.initialize()
        now = utc_now()
        desired_status = "queued" if video.candidate else "skipped"
        terminal_reason = None if video.candidate else video.reason
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            existing = connection.execute(
                """
                SELECT status FROM pipeline_videos
                WHERE source_reference_hash=?
                """,
                (video.source_reference_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO pipeline_videos(
                        source_reference_hash, channel_key, season, week,
                        published_at_utc, duration_seconds, metadata_sha256,
                        status, candidate_reason, terminal_reason,
                        attempt_count, first_discovered_at_utc,
                        last_seen_at_utc, completed_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        video.source_reference_hash,
                        video.channel_key,
                        video.season,
                        video.week,
                        video.published_at_utc,
                        video.duration_seconds,
                        video.metadata_sha256,
                        desired_status,
                        video.reason,
                        terminal_reason,
                        now,
                        now,
                        now if desired_status == "skipped" else None,
                        now,
                    ),
                )
                return desired_status
            current_status = str(existing["status"])
            if current_status == "skipped" and video.candidate:
                connection.execute(
                    """
                    UPDATE pipeline_videos
                    SET season=?,
                        week=?,
                        published_at_utc=?,
                        duration_seconds=?,
                        metadata_sha256=?,
                        status='queued',
                        candidate_reason=?,
                        terminal_reason=NULL,
                        completed_at_utc=NULL,
                        last_seen_at_utc=?,
                        updated_at_utc=?
                    WHERE source_reference_hash=?
                    """,
                    (
                        video.season,
                        video.week,
                        video.published_at_utc,
                        video.duration_seconds,
                        video.metadata_sha256,
                        video.reason,
                        now,
                        now,
                        video.source_reference_hash,
                    ),
                )
                return "queued"
            connection.execute(
                """
                UPDATE pipeline_videos
                SET season=COALESCE(?, season),
                    week=COALESCE(?, week),
                    published_at_utc=COALESCE(?, published_at_utc),
                    duration_seconds=COALESCE(?, duration_seconds),
                    metadata_sha256=?,
                    last_seen_at_utc=?,
                    updated_at_utc=?
                WHERE source_reference_hash=?
                """,
                (
                    video.season,
                    video.week,
                    video.published_at_utc,
                    video.duration_seconds,
                    video.metadata_sha256,
                    now,
                    now,
                    video.source_reference_hash,
                ),
            )
            return current_status

    def release_expired_leases(self) -> int:
        now = utc_now()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            cursor = connection.execute(
                """
                UPDATE pipeline_videos
                SET status='failed',
                    terminal_reason='lease_expired',
                    last_error='Processing lease expired before completion.',
                    lease_token=NULL,
                    lease_expires_at_utc=NULL,
                    next_retry_at_utc=?,
                    updated_at_utc=?
                WHERE status='processing'
                  AND lease_expires_at_utc IS NOT NULL
                  AND lease_expires_at_utc <= ?
                """,
                (now, now, now),
            )
            return cursor.rowcount

    def claim_next(
        self,
        *,
        channel_key: str | None = None,
        season: int | None = None,
        weeks: Iterable[int] = (),
        lease_seconds: int = 7200,
        maximum_attempts: int = 3,
    ) -> dict[str, Any] | None:
        if lease_seconds < 60:
            raise ValueError("lease_seconds must be at least 60")
        requested_weeks = tuple(sorted(set(int(week) for week in weeks)))
        now = datetime.now(UTC)
        now_text = now.isoformat()
        conditions = [
            "("
            "status='queued' OR "
            "(status='failed' AND attempt_count < ? "
            "AND (next_retry_at_utc IS NULL OR next_retry_at_utc <= ?))"
            ")"
        ]
        values: list[Any] = [maximum_attempts, now_text]
        if channel_key:
            conditions.append("channel_key=?")
            values.append(channel_key)
        if season is not None:
            conditions.append("season=?")
            values.append(season)
        if requested_weeks:
            placeholders = ",".join("?" for _ in requested_weeks)
            conditions.append(f"week IN ({placeholders})")
            values.extend(requested_weeks)
        lease_token = str(uuid.uuid4())
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            row = connection.execute(
                f"""
                SELECT * FROM pipeline_videos
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(published_at_utc, first_discovered_at_utc),
                         first_discovered_at_utc,
                         source_reference_hash
                LIMIT 1
                """,
                values,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE pipeline_videos
                SET status='processing',
                    attempt_count=attempt_count + 1,
                    lease_token=?,
                    lease_expires_at_utc=?,
                    started_at_utc=?,
                    terminal_reason=NULL,
                    last_error=NULL,
                    updated_at_utc=?
                WHERE source_reference_hash=?
                """,
                (
                    lease_token,
                    lease_expires,
                    now_text,
                    now_text,
                    row["source_reference_hash"],
                ),
            )
            claimed = dict(row)
            claimed.update(
                {
                    "status": "processing",
                    "attempt_count": int(row["attempt_count"]) + 1,
                    "lease_token": lease_token,
                    "lease_expires_at_utc": lease_expires,
                }
            )
            return claimed

    def mark_duplicate(
        self,
        *,
        source_reference_hash: str,
        lease_token: str,
        duplicate_of_hash: str,
        reason: str,
        provider_transcript_id: str | None,
        segment_sha256: str,
        cleanup_warning: str | None = None,
    ) -> None:
        self._finish(
            source_reference_hash=source_reference_hash,
            lease_token=lease_token,
            status="duplicate",
            terminal_reason=reason,
            duplicate_of_hash=duplicate_of_hash,
            provider_transcript_id=provider_transcript_id,
            segment_sha256=segment_sha256,
            last_error=cleanup_warning,
        )

    def mark_parsed(
        self,
        *,
        source_reference_hash: str,
        lease_token: str,
        provider_transcript_id: str | None,
        segment_sha256: str,
        review_receipt: dict[str, Any],
        opinion_receipt: dict[str, Any],
    ) -> None:
        self._finish(
            source_reference_hash=source_reference_hash,
            lease_token=lease_token,
            status="parsed",
            terminal_reason="parsed_review_only",
            provider_transcript_id=provider_transcript_id,
            segment_sha256=segment_sha256,
            parser_run_id=review_receipt.get("run_id"),
            extraction_revision_id=review_receipt.get("revision_id"),
            opinion_content_id=opinion_receipt.get("content_id"),
            review_receipt=review_receipt,
            opinion_receipt=opinion_receipt,
        )

    def mark_failed(
        self,
        *,
        source_reference_hash: str,
        lease_token: str,
        error: str,
        retry_delay_seconds: int = 3600,
    ) -> None:
        now = datetime.now(UTC)
        retry = (now + timedelta(seconds=retry_delay_seconds)).isoformat()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            cursor = connection.execute(
                """
                UPDATE pipeline_videos
                SET status='failed',
                    terminal_reason='processing_failed',
                    last_error=?,
                    lease_token=NULL,
                    lease_expires_at_utc=NULL,
                    next_retry_at_utc=?,
                    completed_at_utc=?,
                    updated_at_utc=?
                WHERE source_reference_hash=?
                  AND status='processing'
                  AND lease_token=?
                """,
                (
                    error[:4000],
                    retry,
                    now.isoformat(),
                    now.isoformat(),
                    source_reference_hash,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Video lease is missing or stale")

    def _finish(
        self,
        *,
        source_reference_hash: str,
        lease_token: str,
        status: str,
        terminal_reason: str,
        duplicate_of_hash: str | None = None,
        provider_transcript_id: str | None = None,
        segment_sha256: str | None = None,
        parser_run_id: str | None = None,
        extraction_revision_id: str | None = None,
        opinion_content_id: str | None = None,
        review_receipt: dict[str, Any] | None = None,
        opinion_receipt: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("Finish status must be terminal")
        now = utc_now()
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            cursor = connection.execute(
                """
                UPDATE pipeline_videos
                SET status=?,
                    terminal_reason=?,
                    duplicate_of_hash=?,
                    provider_transcript_id=?,
                    segment_sha256=?,
                    parser_run_id=?,
                    extraction_revision_id=?,
                    opinion_content_id=?,
                    review_receipt_json=?,
                    opinion_receipt_json=?,
                    last_error=?,
                    lease_token=NULL,
                    lease_expires_at_utc=NULL,
                    next_retry_at_utc=NULL,
                    completed_at_utc=?,
                    updated_at_utc=?
                WHERE source_reference_hash=?
                  AND status='processing'
                  AND lease_token=?
                """,
                (
                    status,
                    terminal_reason,
                    duplicate_of_hash,
                    provider_transcript_id,
                    segment_sha256,
                    parser_run_id,
                    extraction_revision_id,
                    opinion_content_id,
                    json.dumps(review_receipt, sort_keys=True)
                    if review_receipt is not None
                    else None,
                    json.dumps(opinion_receipt, sort_keys=True)
                    if opinion_receipt is not None
                    else None,
                    last_error[:4000] if last_error else None,
                    now,
                    now,
                    source_reference_hash,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Video lease is missing or stale")

    def save_fingerprint(
        self,
        *,
        source_reference_hash: str,
        transcript: dict[str, Any],
        shingle_size: int = 5,
    ) -> dict[str, int]:
        token_count, hashes = transcript_fingerprint(
            transcript, shingle_size=shingle_size
        )
        encoded = encode_hashes(hashes)
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            connection.execute(
                """
                INSERT INTO pipeline_fingerprints(
                    source_reference_hash, token_count, shingle_size,
                    shingle_count, compressed_hashes, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_reference_hash) DO UPDATE SET
                    token_count=excluded.token_count,
                    shingle_size=excluded.shingle_size,
                    shingle_count=excluded.shingle_count,
                    compressed_hashes=excluded.compressed_hashes,
                    created_at_utc=excluded.created_at_utc
                """,
                (
                    source_reference_hash,
                    token_count,
                    shingle_size,
                    len(hashes),
                    encoded,
                    utc_now(),
                ),
            )
        return {"token_count": token_count, "shingle_count": len(hashes)}

    def find_duplicate(
        self,
        *,
        channel_key: str,
        source_reference_hash: str,
        transcript: dict[str, Any],
        threshold: float = 0.82,
        minimum_shingles: int = 20,
        shingle_size: int = 5,
    ) -> dict[str, Any] | None:
        _, candidate = transcript_fingerprint(
            transcript, shingle_size=shingle_size
        )
        if len(candidate) < minimum_shingles:
            return None
        with connect(self.path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT fingerprint.source_reference_hash,
                       fingerprint.compressed_hashes
                FROM pipeline_fingerprints fingerprint
                JOIN pipeline_videos video
                  ON video.source_reference_hash=
                     fingerprint.source_reference_hash
                WHERE video.channel_key=?
                  AND video.source_reference_hash != ?
                  AND video.status IN ('parsed', 'duplicate')
                """,
                (channel_key, source_reference_hash),
            ).fetchall()
        best = None
        for row in rows:
            existing = decode_hashes(row["compressed_hashes"])
            if not existing:
                continue
            overlap = len(candidate.intersection(existing))
            containment = overlap / min(len(candidate), len(existing))
            if best is None or containment > best["containment"]:
                best = {
                    "source_reference_hash": row["source_reference_hash"],
                    "containment": containment,
                    "overlap_shingles": overlap,
                }
        if best is None or best["containment"] < threshold:
            return None
        return best

    def start_run(
        self,
        *,
        mode: str,
        channel_key: str | None,
        season: int | None,
        weeks: Iterable[int],
    ) -> str:
        run_id = str(uuid.uuid4())
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            connection.execute(
                """
                INSERT INTO pipeline_runs(
                    pipeline_run_id, mode, channel_key, season,
                    weeks_json, status, started_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    mode,
                    channel_key,
                    season,
                    json.dumps(sorted(set(int(week) for week in weeks))),
                    utc_now(),
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        counts: dict[str, int],
        error: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("Pipeline run status is unsupported")
        with connect(self.path) as connection, transaction(
            connection, immediate=True
        ):
            connection.execute(
                """
                UPDATE pipeline_runs
                SET status=?,
                    discovered_count=?,
                    queued_count=?,
                    processed_count=?,
                    skipped_count=?,
                    duplicate_count=?,
                    failed_count=?,
                    error=?,
                    finished_at_utc=?
                WHERE pipeline_run_id=?
                """,
                (
                    status,
                    counts.get("discovered", 0),
                    counts.get("queued", 0),
                    counts.get("processed", 0),
                    counts.get("skipped", 0),
                    counts.get("duplicate", 0),
                    counts.get("failed", 0),
                    error,
                    utc_now(),
                    run_id,
                ),
            )

    def status(self) -> dict[str, Any]:
        self.initialize()
        with connect(self.path, read_only=True) as connection:
            channels = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT channel_key, display_name, program_name, sport,
                           season, expected_speakers_json, active,
                           updated_at_utc
                    FROM pipeline_channels
                    ORDER BY channel_key
                    """
                )
            ]
            statuses = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT channel_key, status, COUNT(*) AS count
                    FROM pipeline_videos
                    GROUP BY channel_key, status
                    ORDER BY channel_key, status
                    """
                )
            ]
            latest_runs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT pipeline_run_id, mode, channel_key, season,
                           weeks_json, status, discovered_count,
                           queued_count, processed_count, skipped_count,
                           duplicate_count, failed_count, error,
                           started_at_utc, finished_at_utc
                    FROM pipeline_runs
                    ORDER BY started_at_utc DESC
                    LIMIT 10
                    """
                )
            ]
        for channel in channels:
            channel["expected_speakers"] = json.loads(
                channel.pop("expected_speakers_json")
            )
            channel["active"] = bool(channel["active"])
        for run in latest_runs:
            run["weeks"] = json.loads(run.pop("weeks_json"))
        return {
            "channels": channels,
            "video_statuses": statuses,
            "latest_runs": latest_runs,
        }

    def video(self, source_reference_hash: str) -> dict[str, Any] | None:
        with connect(self.path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM pipeline_videos
                WHERE source_reference_hash=?
                """,
                (source_reference_hash,),
            ).fetchone()
        return dict(row) if row is not None else None


def transcript_fingerprint(
    transcript: dict[str, Any], *, shingle_size: int = 5
) -> tuple[int, set[int]]:
    if shingle_size < 2:
        raise ValueError("shingle_size must be at least 2")
    text = " ".join(
        str(utterance.get("text") or "")
        for utterance in transcript.get("utterances") or []
    )
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    hashes = set()
    for index in range(max(0, len(tokens) - shingle_size + 1)):
        shingle = "\x1f".join(tokens[index : index + shingle_size])
        digest = __import__("hashlib").blake2b(
            shingle.encode("utf-8"), digest_size=8
        ).digest()
        hashes.add(struct.unpack(">Q", digest)[0])
    return len(tokens), hashes


def encode_hashes(hashes: set[int]) -> bytes:
    packed = b"".join(struct.pack(">Q", value) for value in sorted(hashes))
    return zlib.compress(packed, level=9)


def decode_hashes(payload: bytes) -> set[int]:
    unpacked = zlib.decompress(payload)
    if len(unpacked) % 8:
        raise ValueError("Fingerprint payload has invalid length")
    return {
        struct.unpack(">Q", unpacked[index : index + 8])[0]
        for index in range(0, len(unpacked), 8)
    }
