from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_AUDIO_MIME_TYPES = {
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


@dataclass(frozen=True)
class TelegramAudio:
    media: Any
    kind: str
    mime_type: str
    duration_seconds: int | None
    suffix: str


def telegram_audio(message: Any) -> TelegramAudio | None:
    if message.voice:
        mime_type = str(message.voice.mime_type or "audio/ogg").lower()
        return TelegramAudio(
            media=message.voice,
            kind="voice",
            mime_type=mime_type,
            duration_seconds=int(message.voice.duration),
            suffix=SUPPORTED_AUDIO_MIME_TYPES.get(mime_type, ".ogg"),
        )
    if message.audio:
        mime_type = str(message.audio.mime_type or "audio/mpeg").lower()
        return TelegramAudio(
            media=message.audio,
            kind="audio",
            mime_type=mime_type,
            duration_seconds=(
                int(message.audio.duration)
                if message.audio.duration is not None
                else None
            ),
            suffix=SUPPORTED_AUDIO_MIME_TYPES.get(mime_type, ".audio"),
        )
    if message.document:
        mime_type = str(message.document.mime_type or "").lower()
        if not mime_type.startswith("audio/"):
            return None
        return TelegramAudio(
            media=message.document,
            kind="document",
            mime_type=mime_type,
            duration_seconds=None,
            suffix=SUPPORTED_AUDIO_MIME_TYPES.get(mime_type, ".audio"),
        )
    return None


def audio_review_prompt(
    collection: dict[str, Any],
    items: list[dict[str, Any]],
) -> str:
    clips = []
    for index, item in enumerate(items, start=1):
        duration = item.get("duration_seconds")
        end_instruction = (
            str(duration)
            if duration is not None
            else "determine the exact duration with ffprobe before transcription"
        )
        clips.append(
            f"{index}. Audio path: {item['local_path']}\n"
            "   Start timestamp: 0\n"
            f"   End timestamp: {end_instruction}\n"
            f"   MIME type: {item['mime_type']}"
        )
    clip_text = "\n".join(clips)
    return f"""Read and follow
`www/.claude/skills/nfl-audio-parser/SKILL.md` before doing this work.

The user sent the following private Telegram audio clips as their own Week
{collection['week']} NFL commentary. Treat the supported speaker identity as
AK because the authenticated user explicitly supplied the clips as their own;
do not use voice recognition. Process every clip in order.

Season: {collection['season']}
Week: {collection['week']}

{clip_text}

For each clip, transcribe only the complete bounded file with the checked-in
NFL audio parser and load the authoritative production schedule for the
requested season/week. Combine the clips into one chronological, review-only
analysis of what AK thinks about every game on that week's slate.

For each game report:
- the affirmative side, total, moneyline, prop, pass, or qualitative lean AK
  actually stated;
- the exact line, projected score, confidence, condition, and reasoning only
  when explicitly stated;
- exact supporting transcript quotations with timestamps;
- whether the position is active, retracted, conditional, ambiguous, or
  missing.

Never infer a pick, line, projected score, or confidence from the schedule,
market, tone of voice, or surrounding analysis. Distinguish a general opinion
from an official wager. Preserve contradictory statements and mark the latest
supported position. List every scheduled game that was not discussed or was
not resolved unambiguously.

Persist only the normalized transcript, authoritative schedule, and
review-only extraction through the nfl-audio-parser staging store as allowed
by the skill. Do not write nfl_leans, celebrity_picks, MOE opinions, Google
Sheets, or any production pick store. Do not treat this request as approval to
make official AK submissions.

Return a compact slate-ordered review with exact evidence, unresolved items,
and the staging run/revision receipts. End by stating that official intake or
MOE generation requires a separate explicit approval after AK reviews the
draft.
"""


def remove_audio_files(paths: list[str]) -> list[str]:
    errors: list[str] = []
    parents: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        parents.add(path.parent)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            errors.append(f"{path}: {type(error).__name__}")
    for parent in sorted(
        parents, key=lambda value: len(value.parts), reverse=True
    ):
        try:
            parent.rmdir()
        except OSError:
            continue
    return errors
