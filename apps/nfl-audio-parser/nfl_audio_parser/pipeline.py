from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .channel_registry import (
    CandidateDecision,
    ChannelConfig,
    classify_candidate,
    load_registry,
)
from .opinions import OpinionStore, source_reference
from .pipeline_store import DiscoveredVideo, PipelineStore
from .store import AudioParserStore, canonical_json, sha256_json
from .transcribe import (
    assemblyai_call,
    load_api_key,
    transcribe_youtube,
)


DEFAULT_PIPELINE_DATABASE = Path(
    "/var/lib/nfl-audio-parser/pipeline.sqlite3"
)
DEFAULT_PIPELINE_LOCK = Path("/run/nfl-audio-parser/pipeline.lock")
DEFAULT_STORE_BINARY = Path(
    "/opt/telegram-receptionist/current/.venv/bin/nfl-audio-parser-store"
)
DEFAULT_AGENT_LAUNCHER = Path(
    "/usr/local/libexec/receptionist-agent-runner"
)
DEFAULT_REPOSITORY = Path("/home/receptionist/repos/www")
DEFAULT_CLAUDE_BINARY = Path("/usr/bin/claude")
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"


class AgentBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransientVideo:
    source_reference_hash: str
    source_url: str
    title: str
    description: str
    published_at_utc: str | None
    duration_seconds: float | None
    decision: CandidateDecision
    metadata_sha256: str


def _metadata_hash(
    *,
    title: str,
    description: str,
    published_at_utc: str | None,
    duration_seconds: float | None,
) -> str:
    payload = {
        "title": title,
        "description": description,
        "published_at_utc": published_at_utc,
        "duration_seconds": duration_seconds,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _published_at(info: dict[str, Any]) -> str | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        try:
            parsed = datetime.strptime(upload_date, "%Y%m%d").replace(
                tzinfo=UTC
            )
        except ValueError:
            return None
        return parsed.isoformat()
    return None


class YoutubeChannelDiscovery:
    def discover(self, channel: ChannelConfig) -> list[TransientVideo]:
        if channel.provider != "youtube":
            raise ValueError(f"Unsupported channel provider: {channel.provider}")
        channel_url = (
            f"https://www.youtube.com/{channel.locator.strip('/')}/videos"
        )
        options: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "playlistend": channel.discovery_limit,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
        }
        try:
            with YoutubeDL(options) as downloader:
                listing = downloader.extract_info(channel_url, download=False)
        except DownloadError as error:
            raise RuntimeError(
                f"Unable to enumerate channel {channel.key}: {error}"
            ) from error
        if not isinstance(listing, dict):
            raise RuntimeError(f"Channel {channel.key} returned invalid metadata")
        entries = listing.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError(f"Channel {channel.key} returned no video entries")

        videos = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "").strip()
            if not video_id:
                continue
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            _, reference_hash = source_reference(source_url)
            title = str(entry.get("title") or "")
            description = str(entry.get("description") or "")
            preliminary = classify_candidate(
                channel, title=title, description=description
            )
            should_expand = (
                preliminary.candidate
                or preliminary.reason == "week_unresolved"
                or any(
                    re.search(pattern, title, re.IGNORECASE)
                    for pattern in channel.required_patterns
                )
            )
            info = entry
            if should_expand:
                detail_options = {
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "skip_download": True,
                }
                try:
                    with YoutubeDL(detail_options) as downloader:
                        detailed = downloader.extract_info(
                            source_url, download=False
                        )
                except DownloadError as error:
                    raise RuntimeError(
                        f"Unable to inspect one channel video: {error}"
                    ) from error
                if isinstance(detailed, dict):
                    info = detailed
                    title = str(info.get("title") or title)
                    description = str(info.get("description") or description)
            decision = classify_candidate(
                channel, title=title, description=description
            )
            duration = info.get("duration")
            duration_seconds = (
                float(duration)
                if isinstance(duration, (int, float)) and duration > 0
                else None
            )
            published_at_utc = _published_at(info)
            videos.append(
                TransientVideo(
                    source_reference_hash=reference_hash,
                    source_url=source_url,
                    title=title,
                    description=description,
                    published_at_utc=published_at_utc,
                    duration_seconds=duration_seconds,
                    decision=decision,
                    metadata_sha256=_metadata_hash(
                        title=title,
                        description=description,
                        published_at_utc=published_at_utc,
                        duration_seconds=duration_seconds,
                    ),
                )
            )
        return videos


SCHEDULE_SCRIPT = r"""
import json
import os
from dotenv import load_dotenv

load_dotenv(".env")
load_dotenv(".env.local", override=True)

from nfl_lines import GAME_HEADERS, get_gspread_client

season = int(os.environ["PIPELINE_NFL_SEASON"])
week = int(os.environ["PIPELINE_NFL_WEEK"])
sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
client = get_gspread_client()
worksheet = client.open_by_key(sheet_id).worksheet("nfl_games")
rows = worksheet.get_all_records(expected_headers=GAME_HEADERS)
games = []
for row in rows:
    try:
        row_season = int(row.get("season"))
        row_week = int(row.get("week"))
    except (TypeError, ValueError):
        continue
    if row_season != season or row_week not in {week - 1, week, week + 1}:
        continue
    games.append(
        {
            "event_id": str(row.get("event_id") or ""),
            "season": row_season,
            "week": row_week,
            "status": str(row.get("status") or ""),
            "away_team": str(row.get("away_team") or ""),
            "home_team": str(row.get("home_team") or ""),
            "commence_time_utc": str(row.get("commence_time_utc") or ""),
            "commence_time_et": str(row.get("commence_time_et") or ""),
        }
    )
print(json.dumps({"season": season, "requested_week": week, "games": games}))
"""


def load_authoritative_schedule(season: int, week: int) -> dict[str, Any]:
    environment = (
        f"export PIPELINE_NFL_SEASON={season} "
        f"PIPELINE_NFL_WEEK={week}; "
    )
    command = [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "forwarder",
        "-H",
        "/bin/bash",
        "-lc",
        environment
        + "cd /home/forwarder/app && "
        "/home/forwarder/venv/bin/python -",
    ]
    process = subprocess.run(
        command,
        input=SCHEDULE_SCRIPT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip() or "Authoritative schedule loading failed"
        )
    try:
        schedule = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Schedule loader returned invalid JSON") from error
    if (
        schedule.get("season") != season
        or schedule.get("requested_week") != week
        or not schedule.get("games")
    ):
        raise RuntimeError("Schedule loader returned incomplete week context")
    return schedule


def _job_prompt(job_path: Path, validation_error: str | None = None) -> str:
    correction = ""
    if validation_error:
        correction = (
            "\nA previous extraction failed validation. Correct the existing "
            "output files before validating again. Validation error:\n"
            f"{validation_error}\n"
        )
    return f"""
Process one unattended review-only NFL transcript extraction.

Read the canonical workflow at
/home/receptionist/repos/www/.claude/skills/nfl-audio-parser/SKILL.md.
The pipeline has already completed bounded transcription and authoritative
schedule loading. Read the job specification at {job_path}. Follow the
workflow's speaker identification, exact-quote, pick extraction, conviction,
and review-only rules. Do not download media, transcribe again, import
production celebrity picks, modify repository files, or ask questions.

Write all requested files at the exact paths in the job specification:
- ingest_output: canonical nfl_audio_parser_ingest_v1 envelope.
- opinions_output: sports_opinions_v1 payload for every active, conditional,
  passed, or retracted opinion.
- markdown_output: compact review grouped by game and speaker.

For this channel, map the configured expected speakers only when transcript
evidence supports the names. Capture every affirmative pick, lean, conditional,
parlay/teaser leg, pass, retraction, and meaningful line-dependent change.
Every canonical review pick must include exact transcript substrings and
source timestamps. Add a relative_confidence object to each normalized review
pick with:
- within_speaker_video: a non-empty tier and one-based rank versus that
  speaker's other picks in this video.
- versus_cohost: a non-empty relation describing aligned/opposed/solo and
  relative explicit-language strength.
Every opinion evidence segment must include its transcript utterance_index and
an exact_text substring from that utterance. Include passes and no-bet views in
opinions_output so the review covers every game materially discussed, not only
the strongest or final-card picks.
Never use volume, cadence, or inferred emotion.

When the files are ready, run:
{sys.executable} -m nfl_audio_parser.pipeline validate-job --job {job_path}
Do not finish until that command succeeds.
{correction}
""".strip()


class AgentExtractor:
    def __init__(
        self,
        *,
        launcher: Path = DEFAULT_AGENT_LAUNCHER,
        repository: Path = DEFAULT_REPOSITORY,
        claude_binary: Path = DEFAULT_CLAUDE_BINARY,
        model: str = DEFAULT_CLAUDE_MODEL,
        timeout_seconds: int = 7200,
    ) -> None:
        self.launcher = launcher
        self.repository = repository
        self.claude_binary = claude_binary
        self.model = model
        self.timeout_seconds = timeout_seconds

    def extract(
        self, job_path: Path, *, validation_error: str | None = None
    ) -> None:
        command = [
            str(self.claude_binary),
            "-p",
            _job_prompt(job_path, validation_error),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--model",
            self.model,
            "--effort",
            "high",
        ]
        request = {
            "repository": str(self.repository),
            "command": command,
            "run_id": str(uuid.uuid4()),
        }
        process = subprocess.run(
            [str(self.launcher)],
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
            check=False,
        )
        if process.returncode == 75:
            raise AgentBusyError("The shared extraction agent is busy")
        if process.returncode != 0:
            stderr = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr or "Extraction agent failed")


def _speaker_validation(
    ingest_payload: dict[str, Any],
    opinions_payload: dict[str, Any],
    expected_speakers: Iterable[str],
) -> dict[str, Any]:
    expected = {name.casefold(): name for name in expected_speakers}
    review = ingest_payload.get("review") or {}
    mapped = {
        str(speaker.get("name")).casefold(): str(speaker.get("name"))
        for speaker in review.get("speakers") or []
        if speaker.get("name")
    }
    picker_names = {
        str(observation.get("picker_name")).casefold(): str(
            observation.get("picker_name")
        )
        for observation in opinions_payload.get("observations") or []
        if observation.get("picker_name")
    }
    missing_mappings = [
        display for key, display in expected.items() if key not in mapped
    ]
    missing_opinions = [
        display for key, display in expected.items() if key not in picker_names
    ]
    unexpected = sorted(
        {
            display
            for key, display in {**mapped, **picker_names}.items()
            if key not in expected
        }
    )
    if missing_mappings:
        raise ValueError(
            "Expected speaker mappings are missing: "
            + ", ".join(missing_mappings)
        )
    if missing_opinions:
        raise ValueError(
            "Expected speakers have no parsed opinions: "
            + ", ".join(missing_opinions)
        )
    return {
        "expected_speakers": list(expected.values()),
        "mapped_speakers": sorted(mapped.values()),
        "opinion_speakers": sorted(picker_names.values()),
        "unexpected_speakers": unexpected,
    }


def validate_job(job_path: Path) -> dict[str, Any]:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    transcript_path = Path(job["transcript_path"])
    schedule_path = Path(job["schedule_path"])
    ingest_path = Path(job["ingest_output"])
    opinions_path = Path(job["opinions_output"])
    markdown_path = Path(job["markdown_output"])
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    ingest = json.loads(ingest_path.read_text(encoding="utf-8"))
    opinions = json.loads(opinions_path.read_text(encoding="utf-8"))
    if sha256_json(ingest.get("transcript")) != sha256_json(transcript):
        raise ValueError("Ingest transcript differs from the pipeline transcript")
    if sha256_json(ingest.get("schedule")) != sha256_json(schedule):
        raise ValueError("Ingest schedule differs from the authoritative schedule")
    picks = ((ingest.get("review") or {}).get("picks") or [])
    for position, pick in enumerate(picks):
        relative = pick.get("relative_confidence")
        if not isinstance(relative, dict):
            raise ValueError(
                f"review.picks[{position}].relative_confidence is required"
            )
        if not isinstance(relative.get("within_speaker_video"), dict):
            raise ValueError(
                f"review.picks[{position}] lacks within-speaker confidence"
            )
        if not isinstance(relative.get("versus_cohost"), dict):
            raise ValueError(
                f"review.picks[{position}] lacks cohost confidence comparison"
            )
        within = relative["within_speaker_video"]
        versus = relative["versus_cohost"]
        if not str(within.get("tier") or "").strip():
            raise ValueError(
                f"review.picks[{position}] lacks within-speaker tier"
            )
        rank = within.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise ValueError(
                f"review.picks[{position}] has invalid within-speaker rank"
            )
        if not str(versus.get("relation") or "").strip():
            raise ValueError(
                f"review.picks[{position}] lacks cohost relation"
            )
    markdown = markdown_path.read_text(encoding="utf-8")
    if not markdown.strip():
        raise ValueError("Markdown review output is empty")
    with tempfile.TemporaryDirectory(
        prefix="nfl-audio-pipeline-validation-"
    ) as directory:
        database = Path(directory) / "validation.sqlite3"
        review_receipt = AudioParserStore(database).ingest(ingest)
        linked_opinions = copy.deepcopy(opinions)
        linked_opinions.setdefault("content", {})[
            "extraction_revision_id"
        ] = review_receipt["revision_id"]
        opinion_receipt = OpinionStore(database).ingest(
            linked_opinions,
            source_url=job["source_url"],
        )
    opinion_quotes_by_speaker: dict[str, set[str]] = {}
    for observation in opinions.get("observations") or []:
        speaker = str(observation.get("picker_name") or "").casefold()
        quotes = opinion_quotes_by_speaker.setdefault(speaker, set())
        for segment in observation.get("evidence_segments") or []:
            exact_text = str(segment.get("exact_text") or "")
            if exact_text:
                quotes.add(exact_text)
    for position, pick in enumerate(picks):
        speaker = str(pick.get("speaker_name") or "").casefold()
        pick_quotes = {
            str(segment.get("exact_text") or "")
            for segment in pick.get("evidence_segments") or []
            if segment.get("exact_text")
        }
        if not pick_quotes.intersection(
            opinion_quotes_by_speaker.get(speaker, set())
        ):
            raise ValueError(
                f"review.picks[{position}] is missing from opinions output"
            )
    speaker_validation = _speaker_validation(
        ingest, opinions, job["expected_speakers"]
    )
    return {
        "review_receipt": review_receipt,
        "opinion_receipt": opinion_receipt,
        "speaker_validation": speaker_validation,
        "pick_count": len(picks),
        "opinion_count": len(opinions.get("observations") or []),
    }


class ProductionIngester:
    def __init__(self, store_binary: Path = DEFAULT_STORE_BINARY) -> None:
        self.store_binary = store_binary

    def _run(self, arguments: list[str], payload: dict[str, Any]) -> dict[str, Any]:
        command = [
            "/usr/bin/sudo",
            "-n",
            "-u",
            "forwarder",
            "-H",
            str(self.store_binary),
            *arguments,
        ]
        process = subprocess.run(
            command,
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if process.returncode != 0:
            error = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "Forwarder parser ingest failed")
        try:
            result = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Forwarder parser ingest returned invalid JSON") from error
        if result.get("ok") is not True:
            raise RuntimeError(str(result.get("error") or "Forwarder ingest failed"))
        result.pop("ok", None)
        return result

    def ingest(
        self,
        *,
        source_url: str,
        ingest_payload: dict[str, Any],
        opinions_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        review_receipt = self._run(["ingest"], ingest_payload)
        linked_opinions = copy.deepcopy(opinions_payload)
        linked_opinions.setdefault("content", {})[
            "extraction_revision_id"
        ] = review_receipt["revision_id"]
        opinion_receipt = self._run(
            ["opinions-ingest", "--source-url", source_url],
            linked_opinions,
        )
        return review_receipt, opinion_receipt


def delete_provider_transcript(transcript_id: str | None) -> str | None:
    if not transcript_id:
        return None
    try:
        assemblyai_call(
            load_api_key(), "DELETE", f"/transcript/{transcript_id}"
        )
    except Exception as error:
        return str(error)
    return None


class PipelineRunner:
    def __init__(
        self,
        *,
        store: PipelineStore,
        channels: tuple[ChannelConfig, ...],
        lock_path: Path = DEFAULT_PIPELINE_LOCK,
        discoverer: YoutubeChannelDiscovery | None = None,
        transcriber: Callable[..., dict[str, Any]] = transcribe_youtube,
        schedule_loader: Callable[[int, int], dict[str, Any]] = (
            load_authoritative_schedule
        ),
        extractor: AgentExtractor | None = None,
        ingester: ProductionIngester | None = None,
        provider_cleanup: Callable[[str | None], str | None] = (
            delete_provider_transcript
        ),
    ) -> None:
        self.store = store
        self.channels = channels
        self.lock_path = lock_path
        self.discoverer = discoverer or YoutubeChannelDiscovery()
        self.transcriber = transcriber
        self.schedule_loader = schedule_loader
        self.extractor = extractor or AgentExtractor()
        self.ingester = ingester or ProductionIngester()
        self.provider_cleanup = provider_cleanup

    def sync_registry(self) -> None:
        for channel in self.channels:
            self.store.sync_channel(channel)

    def discover(
        self,
        *,
        channel_key: str | None = None,
    ) -> tuple[dict[str, TransientVideo], dict[str, int]]:
        self.sync_registry()
        transient: dict[str, TransientVideo] = {}
        counts = {"discovered": 0, "queued": 0, "skipped": 0}
        for channel in self.channels:
            if not channel.active or (
                channel_key is not None and channel.key != channel_key
            ):
                continue
            for video in self.discoverer.discover(channel):
                counts["discovered"] += 1
                transient[video.source_reference_hash] = video
                status = self.store.record_discovery(
                    DiscoveredVideo(
                        source_reference_hash=video.source_reference_hash,
                        channel_key=channel.key,
                        season=video.decision.season,
                        week=video.decision.week,
                        published_at_utc=video.published_at_utc,
                        duration_seconds=video.duration_seconds,
                        metadata_sha256=video.metadata_sha256,
                        candidate=video.decision.candidate,
                        reason=video.decision.reason,
                    )
                )
                if status == "queued":
                    counts["queued"] += 1
                elif status == "skipped":
                    counts["skipped"] += 1
        return transient, counts

    def run(
        self,
        *,
        mode: str,
        channel_key: str | None = None,
        season: int | None = None,
        weeks: Iterable[int] = (),
        max_items: int = 8,
    ) -> dict[str, Any]:
        if max_items < 0:
            raise ValueError("max_items must not be negative")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another channel pipeline run is active") from error
            return self._run_locked(
                mode=mode,
                channel_key=channel_key,
                season=season,
                weeks=tuple(weeks),
                max_items=max_items,
            )

    def _run_locked(
        self,
        *,
        mode: str,
        channel_key: str | None,
        season: int | None,
        weeks: tuple[int, ...],
        max_items: int,
    ) -> dict[str, Any]:
        self.store.release_expired_leases()
        run_id = self.store.start_run(
            mode=mode,
            channel_key=channel_key,
            season=season,
            weeks=weeks,
        )
        counts = {
            "discovered": 0,
            "queued": 0,
            "processed": 0,
            "skipped": 0,
            "duplicate": 0,
            "failed": 0,
        }
        try:
            transient, discovery_counts = self.discover(
                channel_key=channel_key
            )
            counts.update(discovery_counts)
            channel_by_key = {channel.key: channel for channel in self.channels}
            for _ in range(max_items):
                claimed = self.store.claim_next(
                    channel_key=channel_key,
                    season=season,
                    weeks=weeks,
                )
                if claimed is None:
                    break
                video = transient.get(claimed["source_reference_hash"])
                channel = channel_by_key[claimed["channel_key"]]
                try:
                    if video is None:
                        raise RuntimeError(
                            "Queued video is absent from the current channel snapshot"
                        )
                    outcome = self._process_one(channel, claimed, video)
                    counts[outcome] += 1
                except AgentBusyError as error:
                    self.store.mark_failed(
                        source_reference_hash=claimed["source_reference_hash"],
                        lease_token=claimed["lease_token"],
                        error=str(error),
                        retry_delay_seconds=900,
                    )
                    counts["failed"] += 1
                    break
                except Exception as error:
                    self.store.mark_failed(
                        source_reference_hash=claimed["source_reference_hash"],
                        lease_token=claimed["lease_token"],
                        error=str(error),
                    )
                    counts["failed"] += 1
            self.store.finish_run(run_id, status="succeeded", counts=counts)
        except Exception as error:
            self.store.finish_run(
                run_id,
                status="failed",
                counts=counts,
                error=str(error),
            )
            raise
        return {"pipeline_run_id": run_id, **counts}

    def _process_one(
        self,
        channel: ChannelConfig,
        claimed: dict[str, Any],
        video: TransientVideo,
    ) -> str:
        duration = video.duration_seconds
        if duration is None or duration <= 1:
            raise ValueError("Video duration is unavailable or too short")
        end_seconds = max(1.0, duration - 0.5)
        transcript_id = None
        with tempfile.TemporaryDirectory(
            prefix="nfl-audio-channel-job-"
        ) as directory:
            job_dir = Path(directory)
            try:
                transcript = self.transcriber(
                    video.source_url,
                    start_timestamp="0",
                    end_timestamp=f"{end_seconds:.3f}",
                )
                transcript_id = transcript.get("provider_transcript_id")
                duplicate = self.store.find_duplicate(
                    channel_key=channel.key,
                    source_reference_hash=video.source_reference_hash,
                    transcript=transcript,
                )
                if duplicate is not None:
                    self.store.save_fingerprint(
                        source_reference_hash=video.source_reference_hash,
                        transcript=transcript,
                    )
                    cleanup_warning = self.provider_cleanup(transcript_id)
                    self.store.mark_duplicate(
                        source_reference_hash=video.source_reference_hash,
                        lease_token=claimed["lease_token"],
                        duplicate_of_hash=duplicate["source_reference_hash"],
                        reason=(
                            "transcript_containment="
                            f"{duplicate['containment']:.4f}"
                        ),
                        provider_transcript_id=transcript_id,
                        segment_sha256=transcript["segment_sha256"],
                        cleanup_warning=cleanup_warning,
                    )
                    return "duplicate"

                week = claimed.get("week")
                season = claimed.get("season")
                if not isinstance(season, int) or not isinstance(week, int):
                    raise ValueError("Candidate season/week is unresolved")
                schedule = self.schedule_loader(season, week)
                transcript_path = job_dir / "transcript.json"
                schedule_path = job_dir / "schedule.json"
                ingest_path = job_dir / "ingest.json"
                opinions_path = job_dir / "opinions.json"
                markdown_path = job_dir / "picks-review.md"
                transcript_path.write_text(
                    json.dumps(transcript, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                schedule_path.write_text(
                    json.dumps(schedule, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                job = {
                    "schema_version": "nfl_audio_channel_job_v1",
                    "channel_key": channel.key,
                    "program_name": channel.program_name,
                    "expected_speakers": list(channel.expected_speakers),
                    "published_at_utc": video.published_at_utc,
                    "season": season,
                    "week": week,
                    "source_start_seconds": 0.0,
                    "source_end_seconds": end_seconds,
                    "source_url": video.source_url,
                    "transcript_path": str(transcript_path),
                    "schedule_path": str(schedule_path),
                    "ingest_output": str(ingest_path),
                    "opinions_output": str(opinions_path),
                    "markdown_output": str(markdown_path),
                }
                job_path = job_dir / "job.json"
                job_path.write_text(
                    json.dumps(job, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                validation_error = None
                validation = None
                for attempt in range(2):
                    self.extractor.extract(
                        job_path, validation_error=validation_error
                    )
                    try:
                        validation = validate_job(job_path)
                        break
                    except (
                        FileNotFoundError,
                        json.JSONDecodeError,
                        KeyError,
                        ValueError,
                    ) as error:
                        validation_error = str(error)
                        if attempt == 1:
                            raise
                assert validation is not None
                ingest_payload = json.loads(
                    ingest_path.read_text(encoding="utf-8")
                )
                opinions_payload = json.loads(
                    opinions_path.read_text(encoding="utf-8")
                )
                review_receipt, opinion_receipt = self.ingester.ingest(
                    source_url=video.source_url,
                    ingest_payload=ingest_payload,
                    opinions_payload=opinions_payload,
                )
                cleanup_error = self.provider_cleanup(transcript_id)
                if cleanup_error:
                    review_receipt["provider_cleanup_warning"] = cleanup_error
                review_receipt["speaker_validation"] = validation[
                    "speaker_validation"
                ]
                self.store.save_fingerprint(
                    source_reference_hash=video.source_reference_hash,
                    transcript=transcript,
                )
                self.store.mark_parsed(
                    source_reference_hash=video.source_reference_hash,
                    lease_token=claimed["lease_token"],
                    provider_transcript_id=transcript_id,
                    segment_sha256=transcript["segment_sha256"],
                    review_receipt=review_receipt,
                    opinion_receipt=opinion_receipt,
                )
                return "processed"
            except Exception as error:
                cleanup_error = self.provider_cleanup(transcript_id)
                if cleanup_error:
                    raise RuntimeError(
                        f"{error}; provider transcript cleanup failed: "
                        f"{cleanup_error}"
                    ) from error
                raise


def parse_weeks(value: str) -> tuple[int, ...]:
    weeks = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError("Week range is reversed")
            weeks.update(range(start, end + 1))
        else:
            weeks.add(int(token))
    if any(week < 1 or week > 22 for week in weeks):
        raise ValueError("Weeks must be between 1 and 22")
    return tuple(sorted(weeks))


def _pipeline_database(value: str | None) -> Path:
    return Path(
        value
        or os.getenv("NFL_AUDIO_PIPELINE_DB", "")
        or DEFAULT_PIPELINE_DATABASE
    ).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and process configured sports-pick channels."
    )
    parser.add_argument("--database")
    parser.add_argument("--registry")
    parser.add_argument("--lock-path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("seed")
    subparsers.add_parser("status")
    discover = subparsers.add_parser("discover")
    discover.add_argument("--channel")
    run = subparsers.add_parser("run")
    run.add_argument("--channel")
    run.add_argument("--max-items", type=int, default=8)
    backfill = subparsers.add_parser("backfill")
    backfill.add_argument("--channel", required=True)
    backfill.add_argument("--season", type=int, required=True)
    backfill.add_argument("--weeks", required=True)
    backfill.add_argument("--max-items", type=int, default=100)
    validate = subparsers.add_parser("validate-job")
    validate.add_argument("--job", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-job":
            result: Any = {
                "ok": True,
                **validate_job(Path(args.job).expanduser().resolve()),
            }
        else:
            channels = load_registry(
                Path(args.registry).expanduser().resolve()
                if args.registry
                else None
            )
            store = PipelineStore(_pipeline_database(args.database))
            runner = PipelineRunner(
                store=store,
                channels=channels,
                lock_path=Path(
                    args.lock_path or DEFAULT_PIPELINE_LOCK
                ).expanduser(),
            )
            if args.command in {"init", "seed"}:
                runner.sync_registry()
                result = {"ok": True, **store.status()}
            elif args.command == "status":
                result = {"ok": True, **store.status()}
            elif args.command == "discover":
                _, counts = runner.discover(channel_key=args.channel)
                result = {"ok": True, **counts}
            elif args.command == "run":
                result = {
                    "ok": True,
                    **runner.run(
                        mode="daily",
                        channel_key=args.channel,
                        max_items=args.max_items,
                    ),
                }
            elif args.command == "backfill":
                result = {
                    "ok": True,
                    **runner.run(
                        mode="backfill",
                        channel_key=args.channel,
                        season=args.season,
                        weeks=parse_weeks(args.weeks),
                        max_items=args.max_items,
                    ),
                }
            else:
                raise AssertionError(f"Unhandled command: {args.command}")
    except (
        AgentBusyError,
        DownloadError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
