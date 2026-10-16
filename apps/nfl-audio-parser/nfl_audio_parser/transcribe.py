from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .opinions import SPORTS, source_reference

ASSEMBLYAI_API = "https://api.assemblyai.com/v2"
DEFAULT_CONFIG = Path.home() / ".config" / "nfl-audio-parser" / "env"
YOUTUBE_HOSTS = {
    "m.youtube.com",
    "music.youtube.com",
    "www.youtube.com",
    "youtu.be",
    "youtube.com",
}


def parse_timestamp(value: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("Timestamp must not be empty")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Unsupported timestamp: {value}")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as error:
        raise ValueError(f"Unsupported timestamp: {value}") from error
    if any(number < 0 for number in numbers):
        raise ValueError(f"Timestamp must not be negative: {value}")
    if len(numbers) == 1:
        return numbers[0]
    if any(number >= 60 for number in numbers[1:]):
        raise ValueError(f"Timestamp minutes and seconds must be below 60: {value}")
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    return hours * 3600 + minutes * 60 + seconds


def load_api_key(config_path: Path = DEFAULT_CONFIG) -> str:
    configured = os.getenv("ASSEMBLYAI_API_KEY", "").strip()
    if configured:
        return configured
    if not config_path.is_file():
        raise ValueError(
            "ASSEMBLYAI_API_KEY is unset and the private config file is missing"
        )
    mode = config_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f"AssemblyAI config must be mode 0600: {config_path}")
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "ASSEMBLYAI_API_KEY":
            configured = value.strip()
            if configured:
                return configured
    raise ValueError("ASSEMBLYAI_API_KEY is empty")


def media_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(process.stdout.strip())


def clip_audio(
    source: Path,
    destination: Path,
    *,
    start_seconds: float,
    end_seconds: float,
) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{end_seconds - start_seconds:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(destination),
        ],
        check=True,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_youtube_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("YouTube URL must use HTTP or HTTPS")
    if (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise ValueError("URL must point to youtube.com or youtu.be")
    if not parsed.path.strip("/"):
        raise ValueError("YouTube URL must identify a video")
    return value.strip()


def download_youtube_audio(url: str, directory: Path) -> Path:
    url = validate_youtube_url(url)
    output_template = str(directory / "source.%(ext)s")
    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            if not isinstance(info, dict) or info.get("_type") == "playlist":
                raise ValueError("YouTube URL must identify one video")
            downloaded = Path(downloader.prepare_filename(info))
    except DownloadError as error:
        raise RuntimeError(f"Unable to download YouTube audio: {error}") from error
    if downloaded.is_file():
        return downloaded
    candidates = sorted(
        path
        for path in directory.glob("source.*")
        if path.is_file() and path.suffix not in {".part", ".ytdl"}
    )
    if len(candidates) != 1:
        raise RuntimeError("Unable to locate downloaded YouTube audio")
    return candidates[0]


def assemblyai_call(
    api_key: str,
    method: str,
    path: str,
    *,
    data: dict[str, Any] | None = None,
    raw: bytes | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"authorization": api_key}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["content-type"] = "application/json"
    elif raw is not None:
        body = raw
    request = urllib.request.Request(
        ASSEMBLYAI_API + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        response = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"AssemblyAI {method} {path} failed with HTTP {error.code}: {response}"
        ) from error


def normalize_transcript(
    result: dict[str, Any],
    *,
    source_start_seconds: float,
    source_end_seconds: float,
    segment_sha256: str,
) -> dict[str, Any]:
    utterances: list[dict[str, Any]] = []
    for index, raw_utterance in enumerate(result.get("utterances") or []):
        segment_start = float(raw_utterance["start"]) / 1000
        segment_end = float(raw_utterance["end"]) / 1000
        words = []
        for raw_word in raw_utterance.get("words") or []:
            word_start = float(raw_word["start"]) / 1000
            word_end = float(raw_word["end"]) / 1000
            words.append(
                {
                    "text": str(raw_word["text"]),
                    "segment_start_seconds": word_start,
                    "segment_end_seconds": word_end,
                    "source_start_seconds": source_start_seconds + word_start,
                    "source_end_seconds": source_start_seconds + word_end,
                    "confidence": raw_word.get("confidence"),
                }
            )
        if words:
            segment_start = min(
                segment_start,
                min(word["segment_start_seconds"] for word in words),
            )
            segment_end = max(
                segment_end,
                max(word["segment_end_seconds"] for word in words),
            )
        utterances.append(
            {
                "utterance_index": index,
                "speaker_label": str(raw_utterance["speaker"]),
                "segment_start_seconds": segment_start,
                "segment_end_seconds": segment_end,
                "source_start_seconds": source_start_seconds + segment_start,
                "source_end_seconds": source_start_seconds + segment_end,
                "confidence": raw_utterance.get("confidence"),
                "text": str(raw_utterance["text"]),
                "words": words,
            }
        )
    if not utterances:
        raise ValueError("AssemblyAI completed without diarized utterances")
    return {
        "provider": "assemblyai",
        "provider_transcript_id": str(result["id"]),
        "segment_sha256": segment_sha256,
        "source_start_seconds": source_start_seconds,
        "source_end_seconds": source_end_seconds,
        "audio_duration_seconds": result.get("audio_duration"),
        "language": result.get("language_code") or "en",
        "utterances": utterances,
    }


def transcribe(
    source: Path,
    *,
    start_timestamp: str,
    end_timestamp: str,
    config_path: Path = DEFAULT_CONFIG,
    poll_seconds: float = 5,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Audio file does not exist: {source}")
    start_seconds = parse_timestamp(start_timestamp)
    end_seconds = parse_timestamp(end_timestamp)
    duration = media_duration(source)
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("End timestamp must be after start timestamp")
    if end_seconds > duration + 0.5:
        raise ValueError(
            f"End timestamp {end_seconds:.3f}s exceeds audio duration {duration:.3f}s"
        )
    api_key = load_api_key(config_path)
    with tempfile.TemporaryDirectory(prefix="nfl-audio-parser-") as directory:
        clip = Path(directory) / "segment.mp3"
        clip_audio(
            source,
            clip,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        clipped_duration = media_duration(clip)
        expected_duration = end_seconds - start_seconds
        if abs(clipped_duration - expected_duration) > 1:
            raise RuntimeError(
                f"Clipped duration {clipped_duration:.3f}s differs from "
                f"expected {expected_duration:.3f}s"
            )
        segment_sha256 = file_sha256(clip)
        upload = assemblyai_call(
            api_key,
            "POST",
            "/upload",
            raw=clip.read_bytes(),
        )
        job = assemblyai_call(
            api_key,
            "POST",
            "/transcript",
            data={
                "audio_url": upload["upload_url"],
                "speaker_labels": True,
                "punctuate": True,
                "format_text": True,
            },
        )
        transcript_id = str(job["id"])
        while True:
            result = assemblyai_call(
                api_key, "GET", f"/transcript/{transcript_id}"
            )
            status = result.get("status")
            if status == "completed":
                return normalize_transcript(
                    result,
                    source_start_seconds=start_seconds,
                    source_end_seconds=end_seconds,
                    segment_sha256=segment_sha256,
                )
            if status == "error":
                raise RuntimeError(
                    f"AssemblyAI transcription failed: {result.get('error')}"
                )
            time.sleep(poll_seconds)


def transcribe_youtube(
    url: str,
    *,
    start_timestamp: str,
    end_timestamp: str,
    config_path: Path = DEFAULT_CONFIG,
    poll_seconds: float = 5,
) -> dict[str, Any]:
    start_seconds = parse_timestamp(start_timestamp)
    end_seconds = parse_timestamp(end_timestamp)
    if end_seconds <= start_seconds:
        raise ValueError("End timestamp must be after start timestamp")
    with tempfile.TemporaryDirectory(
        prefix="nfl-audio-parser-youtube-"
    ) as directory:
        source = download_youtube_audio(url, Path(directory))
        return transcribe(
            source,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            config_path=config_path,
            poll_seconds=poll_seconds,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clip and transcribe one bounded sports-audio segment."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--audio")
    source.add_argument("--youtube-url")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--sport", choices=sorted(SPORTS))
    parser.add_argument("--games-from")
    parser.add_argument("--games-through")
    parser.add_argument("--games-timezone", default="America/New_York")
    return parser


def _sports_context(args: argparse.Namespace) -> dict[str, str] | None:
    supplied = (args.sport, args.games_from, args.games_through)
    if not any(supplied):
        return None
    if not all(supplied):
        raise ValueError(
            "--sport, --games-from, and --games-through must be provided together"
        )
    try:
        valid_from = date.fromisoformat(args.games_from)
        valid_through = date.fromisoformat(args.games_through)
    except ValueError as error:
        raise ValueError(
            "--games-from and --games-through must be YYYY-MM-DD"
        ) from error
    if valid_through < valid_from:
        raise ValueError("--games-through precedes --games-from")
    try:
        ZoneInfo(args.games_timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("--games-timezone is unknown") from error
    return {
        "sport": args.sport,
        "valid_from_date": valid_from.isoformat(),
        "valid_through_date": valid_through.isoformat(),
        "valid_timezone": args.games_timezone,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sports_context = _sports_context(args)
        if args.youtube_url:
            result = transcribe_youtube(
                args.youtube_url,
                start_timestamp=args.start,
                end_timestamp=args.end,
                config_path=Path(args.config).expanduser(),
            )
            provider, reference_hash = source_reference(args.youtube_url)
            result["source"] = {
                "provider": provider,
                "reference_hash": reference_hash,
            }
        else:
            result = transcribe(
                Path(args.audio),
                start_timestamp=args.start,
                end_timestamp=args.end,
                config_path=Path(args.config).expanduser(),
            )
        if sports_context is not None:
            result["sports_context"] = sports_context
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "provider_transcript_id": result["provider_transcript_id"],
                "segment_sha256": result["segment_sha256"],
                "utterance_count": len(result["utterances"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
