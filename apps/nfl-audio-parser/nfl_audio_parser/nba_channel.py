"""Daily NBA picks from configured YouTube shows into the opinion store.

Each run lists a channel's recent uploads, keeps the public NBA picks shows
whose titles name the configured pickers, and for each new one: downloads the
audio (through ``NBA_CHANNEL_PROXY`` when set, because YouTube refuses
downloads from the VPS address), transcribes it with AssemblyAI, loads that
date's ESPN NBA schedule, has Claude (text only, subscription login) extract
the full-game sides and totals each picker states, validates every quote
against the transcript, and ingests a ``sports_opinions_v1`` payload into the
forwarder's opinion store. Members-only videos are skipped.

Like the rest of this package it never stores video URLs, identifiers, or
titles: run state is keyed by the irreversible source reference hash.
``export`` writes the configured pickers' latest pregame picks and covered
dates to a JSON file for Mel's bait tracker.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .opinions import OpinionStore, source_reference
from .store import CONVICTION_LABELS
from .transcribe import (
    DEFAULT_CONFIG,
    assemblyai_call,
    load_api_key,
    media_duration,
    transcribe,
)


EASTERN = ZoneInfo("America/New_York")
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
DEFAULT_STATE = Path("/var/lib/decision-helper-bot/nba-channel-state.json")
DEFAULT_EXPORT = Path("/var/lib/decision-helper-bot/picker-picks.json")
DEFAULT_LOCK = Path("/var/lib/decision-helper-bot/nba-channel.lock")
DEFAULT_STORE_BINARY = Path(
    "/opt/decision-helper-bot/current/.venv/bin/nfl-audio-parser-store"
)
DEFAULT_CLAUDE_BINARY = "/usr/bin/claude"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
MAX_ATTEMPTS = 3
# A video downloaded minutes after upload can come back cut short while
# YouTube is still processing it (2026-11-13: 177 s of a 502 s show). Audio
# shorter than this share of the reported length fails and is retried.
MIN_AUDIO_SHARE = 0.95
PRIVATE_AVAILABILITY = {"subscriber_only", "premium_only", "needs_auth"}
MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
SUBSCRIPTION_BLOCKED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


@dataclass(frozen=True)
class Channel:
    key: str
    locator: str
    pickers: tuple[str, ...]
    # Program name by the set of pickers a title names.
    programs: dict[frozenset[str], str]


CHANNELS = {
    "docs-sports": Channel(
        key="docs-sports",
        locator=(
            "https://www.youtube.com/channel/UCbxXLZ-uHOY-QrsL1rMbXew/videos"
        ),
        pickers=("Craig Trapp", "Indian Cowboy"),
        programs={
            frozenset({"Craig Trapp", "Indian Cowboy"}): (
                "Doc's Sports Craig Trapp & Indian Cowboy NBA Picks"
            ),
            frozenset({"Craig Trapp"}): (
                "Doc's Sports Craig Trapp & Indian Cowboy NBA Picks"
            ),
            frozenset({"Indian Cowboy"}): "Doc's Sports Indian Cowboy NBA Picks",
        },
    ),
}


@dataclass(frozen=True)
class Candidate:
    url: str
    reference_hash: str
    title: str
    title_date: date
    pickers: tuple[str, ...]
    program_name: str


# --- Discovery -------------------------------------------------------------


def title_date(title: str, year_hint: int) -> date | None:
    """The slate date a title names: ``11/9/26`` or ``November 9th[, 2026]``."""
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", title)
    if numeric:
        month, day, year = (int(part) for part in numeric.groups())
        year = year + 2000 if year < 100 else year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    words = re.search(
        r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:,?\s+(\d{4}))?",
        title,
        flags=re.IGNORECASE,
    )
    if words:
        month = MONTHS[words.group(1).casefold()]
        year = int(words.group(3)) if words.group(3) else year_hint
        try:
            return date(year, month, int(words.group(2)))
        except ValueError:
            return None
    return None


def classify(
    channel: Channel,
    entry: dict[str, Any],
    year_hint: int,
) -> Candidate | None:
    """A public NBA picks show naming the channel's pickers, else None."""
    title = " ".join(str(entry.get("title") or "").split())
    folded = title.casefold()
    if str(entry.get("availability") or "") in PRIVATE_AVAILABILITY:
        return None
    if "premium" in folded or "members" in folded:
        return None
    if not re.search(r"\bnba\b", folded) or re.search(r"\bwnba\b", folded):
        return None
    if "preseason" in folded or "summer league" in folded:
        return None
    pickers = tuple(name for name in channel.pickers if name.casefold() in folded)
    if not pickers:
        return None
    slate = title_date(title, year_hint)
    video_id = str(entry.get("id") or "")
    if slate is None or not video_id:
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    return Candidate(
        url=url,
        reference_hash=source_reference(url)[1],
        title=title,
        title_date=slate,
        pickers=pickers,
        program_name=channel.programs[frozenset(pickers)],
    )


def list_uploads(channel: Channel, limit: int) -> list[dict[str, Any]]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "skip_download": True,
    }
    with YoutubeDL(options) as client:
        info = client.extract_info(channel.locator, download=False)
    return [entry for entry in info.get("entries") or [] if entry]


# --- Media and schedule ------------------------------------------------------


def download_audio(
    url: str, directory: Path, proxy: str | None
) -> tuple[Path, datetime, float | None]:
    """Best audio into ``directory``; returns (path, published time, the
    video's reported length in seconds when YouTube gives one)."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "format": "bestaudio/best",
        "outtmpl": str(directory / "source.%(ext)s"),
    }
    if proxy:
        options["proxy"] = proxy
    try:
        with YoutubeDL(options) as client:
            info = client.extract_info(url, download=True)
    except DownloadError as error:
        message = str(error)
        if proxy:
            message = message.replace(proxy, "<proxy>")
        raise RuntimeError(f"Unable to download YouTube audio: {message}") from None
    published = info.get("release_timestamp") or info.get("timestamp")
    if not published:
        raise RuntimeError("YouTube did not report the video's publish time")
    files = [path for path in directory.iterdir() if path.name.startswith("source.")]
    if len(files) != 1:
        raise RuntimeError("Expected one downloaded audio file")
    reported = info.get("duration")
    return (
        files[0],
        datetime.fromtimestamp(int(published), UTC),
        float(reported) if isinstance(reported, (int, float)) and reported > 0 else None,
    )


def check_complete(duration: float, reported: float | None) -> None:
    """Refuse audio that is clearly shorter than the video."""
    if reported is not None and duration < reported * MIN_AUDIO_SHARE:
        raise RuntimeError(
            f"Downloaded audio is {duration:.0f} s of the video's {reported:.0f} s "
            "(YouTube may still be processing it); retrying on a later run"
        )


def espn_schedule(day: date) -> list[dict[str, Any]]:
    url = f"{ESPN_SCOREBOARD}?dates={day:%Y%m%d}"
    with urllib.request.urlopen(url, timeout=30) as response:
        body = json.load(response)
    events = []
    for event in body.get("events") or []:
        competition = (event.get("competitions") or [{}])[0]
        teams = {
            side.get("homeAway"): side.get("team", {}).get("displayName")
            for side in competition.get("competitors") or []
        }
        if not teams.get("away") or not teams.get("home"):
            continue
        commence = datetime.fromisoformat(str(event["date"]).replace("Z", "+00:00"))
        events.append(
            {
                "event_id": str(event["id"]),
                "sport": "nba",
                "commence_time_utc": commence.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "away_team": teams["away"],
                "home_team": teams["home"],
                "status": "scheduled",
            }
        )
    return events


def candidate_schedule(
    candidate: Candidate,
    published: datetime,
    loader: Callable[[date], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Games the video could be previewing: the title's date, the publish
    date and the day after (ET), limited to games not yet started when the
    video went up."""
    published_day = published.astimezone(EASTERN).date()
    days = sorted(
        {candidate.title_date, published_day, published_day + timedelta(days=1)}
    )
    events: dict[str, dict[str, Any]] = {}
    for day in days:
        for event in loader(day):
            commence = datetime.fromisoformat(
                event["commence_time_utc"].replace("Z", "+00:00")
            )
            if commence > published:
                events[event["event_id"]] = event
    return sorted(events.values(), key=lambda row: (row["commence_time_utc"], row["event_id"]))


# --- Extraction --------------------------------------------------------------


def extraction_prompt(
    candidate: Candidate,
    published: datetime,
    transcript: dict[str, Any],
    schedule: list[dict[str, Any]],
) -> str:
    utterances = [
        {
            "utterance_index": utterance["utterance_index"],
            "speaker_label": utterance.get("speaker_label"),
            "start_seconds": round(float(utterance["source_start_seconds"]), 1),
            "text": utterance["text"],
        }
        for utterance in transcript["utterances"]
    ]
    games = [
        {
            "event_id": event["event_id"],
            "tip_off_et": datetime.fromisoformat(
                event["commence_time_utc"].replace("Z", "+00:00")
            ).astimezone(EASTERN).strftime("%a %b %-d %-I:%M %p"),
            "away_team": event["away_team"],
            "home_team": event["home_team"],
        }
        for event in schedule
    ]
    return f"""
You extract NBA betting picks from one YouTube show transcript for a private
picks tracker. Reply with one JSON object and nothing else.

Show title: {json.dumps(candidate.title)}
Published: {published.astimezone(EASTERN):%a %b %-d %Y %-I:%M %p} ET
Expected hosts: {json.dumps(list(candidate.pickers))}
The title's slate date: {candidate.title_date.isoformat()}

Games the show can be about (ESPN; nothing else counts):
{json.dumps(games, indent=1)}

Transcript utterances (AssemblyAI; speaker labels are diarization guesses and
names or numbers can be misheard):
{json.dumps(utterances, indent=1)}

Rules:
- Map speaker labels to the expected hosts only from transcript evidence
  (self-introductions, who is addressed by name before answering). "IC" is
  Indian Cowboy. A host's name may be misheard (e.g. "Craig Arum" is Craig
  Trapp). If a label can't be mapped with confidence, leave it out.
- Extract every pick a host states as their own for a listed game: full-game
  or period spreads and moneylines (market_family "game_side") and game totals
  (market_family "game_total"). Skip player props, team totals, parlays,
  other sports, and other people's picks they quote.
- direction: the exact team name from the games list for sides, "Over" or
  "Under" for totals. line: the number as stated from that side's point of
  view (underdog +3.5, favorite -3.5); null if they gave none. Do not take a
  number from anywhere but their words. contract_type: "spread", "moneyline"
  (e.g. "they win", "on the money line"), or "total". period: "game", "1h",
  "2h", or "1q".."4q".
- opinion_status: "active"; "conditional" when the pick depends on a stated
  condition (an injury status, a number being available), with condition_text;
  "pass" when they say to stay off the game.
- conviction_score 1-5 from explicit words only: 2 lean/slight/small,
  3 plain "I like/give me/take", 4 "love/really like/strong", 5 "best bet/
  lock/play of the day". hedged: true if they hedge.
- quotes: 1-3 exact substrings copied character for character from one
  utterance's text, with that utterance_index. The shortest span that states
  the pick.
- One entry per distinct pick; if a host repeats the same pick, use one entry
  with both quotes.

JSON shape:
{{"speakers": {{"A": "Craig Trapp"}},
 "picks": [{{"picker_name": "...", "event_id": "...",
   "market_family": "game_side", "contract_type": "spread",
   "period": "game", "direction": "...", "line": 3.5,
   "opinion_status": "active", "condition_text": null,
   "conviction_score": 3, "explicit_markers": ["like"], "hedged": false,
   "quotes": [{{"utterance_index": 4, "exact_text": "..."}}],
   "extraction_confidence": 0.9, "review_flags": []}}],
 "notes": "anything uncertain"}}
""".strip()


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude returned no JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Claude returned non-object JSON")
    return value


def claude_extract(
    prompt: str,
    *,
    binary: str = DEFAULT_CLAUDE_BINARY,
    model: str = DEFAULT_CLAUDE_MODEL,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """One text-only Claude Code call on the subscription login."""
    environment = dict(os.environ)
    for key in SUBSCRIPTION_BLOCKED_ENV:
        environment.pop(key, None)
    environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    environment["DISABLE_AUTOUPDATER"] = "1"
    with tempfile.TemporaryDirectory(prefix="nba-channel-claude-") as workdir:
        process = subprocess.run(
            [
                binary, "-p", prompt,
                "--output-format", "json",
                "--model", model,
                "--effort", "medium",
                "--tools", "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ],
            cwd=workdir,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    try:
        envelope = json.loads(process.stdout)
    except json.JSONDecodeError:
        envelope = {}
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if process.returncode != 0 or envelope.get("is_error") or not isinstance(result, str):
        detail = (process.stderr or str(result or ""))[-800:].strip()
        raise RuntimeError(f"Claude extraction failed: {detail or process.returncode}")
    return parse_json_object(result)


# --- Payload ---------------------------------------------------------------


def _norm_word(value: str) -> str:
    return re.sub(r"[^a-z0-9.+-]", "", value.casefold())


def quote_evidence(
    utterances: dict[int, dict[str, Any]], index: Any, text: Any
) -> dict[str, Any] | None:
    """An evidence segment with word timings when ``text`` is an exact
    substring of the utterance; None otherwise."""
    if not isinstance(index, int) or index not in utterances:
        return None
    text = str(text or "").strip()
    utterance = utterances[index]
    if not text or text not in utterance["text"]:
        return None
    words = utterance.get("words") or []
    target = [_norm_word(word) for word in text.split()]
    target = [word for word in target if word]
    start = float(utterance["source_start_seconds"])
    end = float(utterance["source_end_seconds"])
    normalized = [_norm_word(str(word.get("text") or "")) for word in words]
    for position in range(len(words) - len(target) + 1):
        if target and normalized[position:position + len(target)] == target:
            start = float(words[position]["source_start_seconds"])
            end = float(words[position + len(target) - 1]["source_end_seconds"])
            break
    return {
        "exact_text": text,
        "speaker_label": utterance.get("speaker_label"),
        "utterance_index": index,
        "source_start_seconds": start,
        "source_end_seconds": end,
    }


def build_payload(
    candidate: Candidate,
    published: datetime,
    transcript: dict[str, Any],
    schedule: list[dict[str, Any]],
    extraction: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """The opinions payload (None when no pick survives) and why any
    extracted pick was dropped."""
    utterances = {u["utterance_index"]: u for u in transcript["utterances"]}
    events = {event["event_id"]: event for event in schedule}
    speakers = extraction.get("speakers") or {}
    speaker_flag = (
        "Speakers mapped from transcript evidence: "
        + ", ".join(f"{label}={name}" for label, name in sorted(speakers.items()))
        if isinstance(speakers, dict) and speakers
        else "Speakers mapped from transcript evidence."
    )
    dropped: list[str] = []
    observations: list[dict[str, Any]] = []
    for position, pick in enumerate(extraction.get("picks") or []):
        label = f"pick {position + 1}"
        if not isinstance(pick, dict):
            dropped.append(f"{label}: not an object")
            continue
        picker = pick.get("picker_name")
        if picker not in candidate.pickers:
            dropped.append(f"{label}: picker {picker!r} is not an expected host")
            continue
        event = events.get(str(pick.get("event_id")))
        if event is None:
            dropped.append(f"{label}: unknown game {pick.get('event_id')!r}")
            continue
        family = pick.get("market_family")
        contract = pick.get("contract_type")
        direction = pick.get("direction")
        if family == "game_side":
            if contract not in {"spread", "moneyline"} or direction not in {
                event["away_team"], event["home_team"],
            }:
                dropped.append(f"{label}: bad side {contract!r} {direction!r}")
                continue
        elif family == "game_total":
            if contract != "total" or direction not in {"Over", "Under"}:
                dropped.append(f"{label}: bad total {direction!r}")
                continue
        else:
            dropped.append(f"{label}: unsupported market {family!r}")
            continue
        period = pick.get("period") or "game"
        if period not in {"game", "1h", "2h", "1q", "2q", "3q", "4q"}:
            dropped.append(f"{label}: bad period {period!r}")
            continue
        line = pick.get("line")
        if line is not None and not isinstance(line, (int, float)):
            dropped.append(f"{label}: bad line {line!r}")
            continue
        if contract == "moneyline":
            line = None
        status = pick.get("opinion_status") or "active"
        if status not in {"active", "conditional", "pass"}:
            dropped.append(f"{label}: bad status {status!r}")
            continue
        evidence = [
            segment
            for quote in pick.get("quotes") or []
            if isinstance(quote, dict)
            and (
                segment := quote_evidence(
                    utterances, quote.get("utterance_index"), quote.get("exact_text")
                )
            )
        ]
        if not evidence:
            dropped.append(f"{label}: no quote matches the transcript")
            continue
        score = pick.get("conviction_score")
        score = score if isinstance(score, int) and 1 <= score <= 5 else 3
        condition = pick.get("condition_text") if status == "conditional" else None
        if status == "conditional" and not condition:
            condition = "Stated as conditional."
        if family == "game_total":
            selection = f"{direction} {line:g}" if line is not None else direction
        elif contract == "moneyline":
            selection = f"{direction} ML"
        else:
            selection = f"{direction} {line:+g}" if line is not None else direction
        if period != "game":
            selection += f" ({period.upper()})"
        flags = [speaker_flag]
        flags += [str(flag) for flag in pick.get("review_flags") or [] if flag]
        if line is None and contract != "moneyline":
            flags.append("No number stated.")
        confidence = pick.get("extraction_confidence")
        confidence = (
            float(confidence)
            if isinstance(confidence, (int, float)) and 0 <= confidence <= 1
            else 0.8
        )
        observations.append(
            {
                "observation_index": len(observations) + 1,
                "picker_name": picker,
                "event_id": event["event_id"],
                "market_family": family,
                "contract_type": contract,
                "period": period,
                "source_seconds": evidence[0]["source_start_seconds"],
                "selection": selection,
                "direction": direction,
                "line": float(line) if line is not None else None,
                "opinion_status": status,
                "opinion_stage": "final",
                "conviction": {
                    "score": score,
                    "label": CONVICTION_LABELS[score],
                    # The store requires each marker word in a quote.
                    "explicit_markers": [
                        str(marker)
                        for marker in pick.get("explicit_markers") or []
                        if any(str(marker) in segment["exact_text"] for segment in evidence)
                    ],
                    "hedged": bool(pick.get("hedged")),
                    "conditional": status == "conditional",
                    "condition_text": condition,
                },
                "evidence_segments": evidence,
                "extraction_confidence": confidence,
                "review_flags": flags,
            }
        )
    if not observations:
        return None, dropped
    used = sorted({row["event_id"] for row in observations})
    # The window is the picked games' dates, not the title's: one show was
    # titled with an old date.
    slate_days = {
        datetime.fromisoformat(
            events[event_id]["commence_time_utc"].replace("Z", "+00:00")
        ).astimezone(EASTERN).date()
        for event_id in used
    }
    payload = {
        "schema_version": "sports_opinions_v1",
        "content": {
            "source_provider": "youtube",
            "program_name": candidate.program_name,
            "published_at_utc": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sport": "nba",
            "valid_from_date": min(slate_days).isoformat(),
            "valid_through_date": max(slate_days).isoformat(),
            "valid_timezone": "America/New_York",
            "source_start_seconds": transcript["source_start_seconds"],
            "source_end_seconds": transcript["source_end_seconds"],
            "segment_sha256": transcript["segment_sha256"],
        },
        "events": [events[event_id] for event_id in used],
        "observations": observations,
    }
    return payload, dropped


# --- Store access ------------------------------------------------------------


def forwarder_store(
    arguments: list[str],
    *,
    store_binary: Path,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the opinion-store CLI as the forwarder account, which owns the
    production database."""
    process = subprocess.run(
        ["/usr/bin/sudo", "-n", "-u", "forwarder", "-H", str(store_binary), *arguments],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd="/",
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip()[-800:] or "Opinion store command failed")
    result = json.loads(process.stdout)
    if result.get("ok") is not True:
        raise RuntimeError(str(result.get("error") or "Opinion store command failed"))
    return result


def validate_payload(payload: dict[str, Any], source_url: str) -> None:
    """Ingest into a throwaway store so a bad payload never reaches the
    production database."""
    with tempfile.TemporaryDirectory(prefix="nba-channel-validate-") as directory:
        OpinionStore(Path(directory) / "check.sqlite3").ingest(
            payload, source_url=source_url
        )


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def export_picks(channel: Channel, output: Path, store_binary: Path) -> dict[str, Any]:
    arguments = ["picks-export", "--sport", "nba"]
    for picker in channel.pickers:
        arguments += ["--picker", picker]
    result = forwarder_store(arguments, store_binary=store_binary)
    result.pop("ok", None)
    result["exported_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json_atomic(output, result)
    return {"picks": len(result["picks"]), "output": str(output)}


# --- Runner ----------------------------------------------------------------


class Runner:
    def __init__(
        self,
        *,
        channel: Channel,
        state_path: Path,
        store_binary: Path,
        proxy: str | None,
        claude_binary: str,
        claude_model: str,
        config_path: Path = DEFAULT_CONFIG,
        dry_run: bool = False,
        log: Callable[[str], None] = lambda line: print(line, flush=True),
    ) -> None:
        self.channel = channel
        self.state_path = state_path
        self.store_binary = store_binary
        self.proxy = proxy
        self.claude_binary = claude_binary
        self.claude_model = claude_model
        self.config_path = config_path
        self.dry_run = dry_run
        self.log = log
        self.state: dict[str, dict[str, Any]] = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.is_file()
            else {}
        )

    def save(self) -> None:
        if not self.dry_run:
            write_json_atomic(self.state_path, self.state)

    def mark(self, candidate: Candidate, status: str, **details: Any) -> None:
        previous = self.state.get(candidate.reference_hash, {})
        self.state[candidate.reference_hash] = {
            "slate_date": candidate.title_date.isoformat(),
            "program_name": candidate.program_name,
            "status": status,
            "attempts": previous.get("attempts", 0) + (status == "failed"),
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **details,
        }
        self.save()

    def pending(self, since: date, until: date, scan: int) -> list[Candidate]:
        today = datetime.now(EASTERN).date()
        found: dict[str, Candidate] = {}
        for entry in list_uploads(self.channel, scan):
            candidate = classify(self.channel, entry, today.year)
            if candidate is None or not since <= candidate.title_date <= until:
                continue
            state = self.state.get(candidate.reference_hash) or {}
            if state.get("status") in {"ingested", "no_picks"}:
                continue
            if state.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            found.setdefault(candidate.reference_hash, candidate)
        return sorted(found.values(), key=lambda item: item.title_date)

    def process(self, candidate: Candidate) -> dict[str, Any]:
        transcript_id = None
        try:
            with tempfile.TemporaryDirectory(prefix="nba-channel-") as directory:
                audio, published, reported = download_audio(
                    candidate.url, Path(directory), self.proxy
                )
                duration = media_duration(audio)
                check_complete(duration, reported)
                transcript = transcribe(
                    audio,
                    start_timestamp="0",
                    end_timestamp=f"{max(1.0, duration - 0.25):.3f}",
                    config_path=self.config_path,
                )
            transcript_id = transcript.get("provider_transcript_id")
            schedule = candidate_schedule(candidate, published, espn_schedule)
            if not schedule:
                raise RuntimeError("No NBA games left on the video's slate")
            extraction = claude_extract(
                extraction_prompt(candidate, published, transcript, schedule),
                binary=self.claude_binary,
                model=self.claude_model,
            )
            payload, dropped = build_payload(
                candidate, published, transcript, schedule, extraction
            )
            if payload is None:
                self.mark(candidate, "no_picks", dropped=dropped)
                return {"status": "no_picks", "dropped": dropped}
            validate_payload(payload, candidate.url)
            if self.dry_run:
                return {"status": "dry_run", "payload": payload, "dropped": dropped}
            receipt = forwarder_store(
                ["opinions-ingest", "--source-url", candidate.url, "--input", "-"],
                store_binary=self.store_binary,
                payload=payload,
            )
            self.mark(
                candidate,
                "ingested",
                observations=receipt.get("observation_count"),
                dropped=dropped,
            )
            return {
                "status": "ingested",
                "observations": receipt.get("observation_count"),
                "dropped": dropped,
                "picks": [row["selection"] + " — " + row["picker_name"] for row in payload["observations"]],
            }
        except Exception as error:  # one video's failure must not stop the run
            message = str(error)
            if self.proxy:
                message = message.replace(self.proxy, "<proxy>")
            self.mark(candidate, "failed", error=message[-500:])
            return {"status": "failed", "error": message[-500:]}
        finally:
            if transcript_id:
                try:
                    assemblyai_call(
                        load_api_key(self.config_path),
                        "DELETE",
                        f"/transcript/{transcript_id}",
                    )
                except Exception as error:
                    self.log(f"AssemblyAI cleanup failed: {error}")

    def run(self, since: date, until: date, scan: int, max_items: int) -> list[dict[str, Any]]:
        results = []
        for candidate in self.pending(since, until, scan)[:max_items]:
            outcome = self.process(candidate)
            outcome["slate_date"] = candidate.title_date.isoformat()
            outcome["program_name"] = candidate.program_name
            self.log(json.dumps(outcome, sort_keys=True))
            results.append(outcome)
        return results


# --- CLI ---------------------------------------------------------------------


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest daily NBA picks shows from configured YouTube channels."
    )
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="docs-sports")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--export-path", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--store-binary", type=Path, default=DEFAULT_STORE_BINARY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Process new shows, then export.")
    run.add_argument("--since", type=_date, help="First slate date (ET); default yesterday.")
    run.add_argument("--until", type=_date, help="Last slate date (ET); default tomorrow.")
    run.add_argument("--scan", type=int, default=150, help="Recent uploads to list.")
    run.add_argument("--max-items", type=int, default=6)
    run.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("export", help="Only write the picker export.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    channel = CHANNELS[args.channel]
    lock_path = Path(os.getenv("NBA_CHANNEL_LOCK", str(DEFAULT_LOCK)))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": False, "error": "Another intake run is active"}))
            return 75
        try:
            if args.command == "run":
                today = datetime.now(EASTERN).date()
                runner = Runner(
                    channel=channel,
                    state_path=args.state,
                    store_binary=args.store_binary,
                    proxy=os.getenv("NBA_CHANNEL_PROXY", "").strip() or None,
                    claude_binary=os.getenv("CLAUDE_BINARY", DEFAULT_CLAUDE_BINARY),
                    claude_model=os.getenv("NBA_CHANNEL_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
                    dry_run=args.dry_run,
                )
                results = runner.run(
                    args.since or today - timedelta(days=1),
                    args.until or today + timedelta(days=1),
                    args.scan,
                    args.max_items,
                )
                summary: dict[str, Any] = {
                    "ok": all(row["status"] != "failed" for row in results),
                    "processed": len(results),
                }
                if not args.dry_run:
                    summary["export"] = export_picks(
                        channel, args.export_path, args.store_binary
                    )
            else:
                summary = {
                    "ok": True,
                    "export": export_picks(channel, args.export_path, args.store_binary),
                }
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)[-800:]}))
            return 1
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
