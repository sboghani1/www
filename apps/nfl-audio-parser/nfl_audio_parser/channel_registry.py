from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHANNEL_REGISTRY_SCHEMA = "nfl_audio_channel_registry_v1"


@dataclass(frozen=True)
class SeedReference:
    source_reference_hash: str
    season: int
    week: int
    status: str
    reason: str
    duplicate_of_hash: str | None


@dataclass(frozen=True)
class ChannelConfig:
    key: str
    provider: str
    locator: str
    display_name: str
    program_name: str
    sport: str
    active: bool
    season: int
    expected_speakers: tuple[str, ...]
    required_patterns: tuple[str, ...]
    candidate_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    discovery_limit: int
    seeds: tuple[SeedReference, ...]

    @property
    def reference_hash(self) -> str:
        value = f"{self.provider}-channel:{self.locator.casefold()}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @property
    def config_hash(self) -> str:
        payload = {
            "key": self.key,
            "provider": self.provider,
            "locator": self.locator,
            "display_name": self.display_name,
            "program_name": self.program_name,
            "sport": self.sport,
            "active": self.active,
            "season": self.season,
            "expected_speakers": self.expected_speakers,
            "required_patterns": self.required_patterns,
            "candidate_patterns": self.candidate_patterns,
            "exclude_patterns": self.exclude_patterns,
            "discovery_limit": self.discovery_limit,
            "seeds": [
                {
                    "source_reference_hash": seed.source_reference_hash,
                    "season": seed.season,
                    "week": seed.week,
                    "status": seed.status,
                    "reason": seed.reason,
                    "duplicate_of_hash": seed.duplicate_of_hash,
                }
                for seed in self.seeds
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CandidateDecision:
    candidate: bool
    season: int | None
    week: int | None
    reason: str


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _patterns(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    patterns = tuple(_require_text(item, field) for item in value)
    for pattern in patterns:
        re.compile(pattern, re.IGNORECASE)
    return patterns


def _validate_hash(value: Any, field: str) -> str:
    text = _require_text(value, field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def load_registry(path: Path | None = None) -> tuple[ChannelConfig, ...]:
    registry_path = path or Path(__file__).with_name("channels.json")
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != CHANNEL_REGISTRY_SCHEMA:
        raise ValueError("Channel registry schema_version is unsupported")
    rows = payload.get("channels")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Channel registry must contain channels")
    channels = []
    seen = set()
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"channels[{position}] must be an object")
        key = _require_text(row.get("key"), f"channels[{position}].key")
        if key in seen:
            raise ValueError(f"Duplicate channel key: {key}")
        seen.add(key)
        expected = row.get("expected_speakers")
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"channels[{position}].expected_speakers is required")
        speakers = tuple(
            _require_text(value, f"channels[{position}].expected_speakers")
            for value in expected
        )
        seeds = []
        for seed_position, seed in enumerate(row.get("seeds", [])):
            if not isinstance(seed, dict):
                raise ValueError(
                    f"channels[{position}].seeds[{seed_position}] must be an object"
                )
            status = _require_text(
                seed.get("status"),
                f"channels[{position}].seeds[{seed_position}].status",
            )
            if status not in {"parsed", "duplicate", "skipped"}:
                raise ValueError("Seed status must be parsed, duplicate, or skipped")
            duplicate_of_hash = seed.get("duplicate_of_hash")
            if duplicate_of_hash is not None:
                duplicate_of_hash = _validate_hash(
                    duplicate_of_hash,
                    (
                        f"channels[{position}].seeds[{seed_position}]"
                        ".duplicate_of_hash"
                    ),
                )
            if status == "duplicate" and duplicate_of_hash is None:
                raise ValueError("Duplicate seeds require duplicate_of_hash")
            if status != "duplicate" and duplicate_of_hash is not None:
                raise ValueError(
                    "Only duplicate seeds may declare duplicate_of_hash"
                )
            seeds.append(
                SeedReference(
                    source_reference_hash=_validate_hash(
                        seed.get("source_reference_hash"),
                        (
                            f"channels[{position}].seeds[{seed_position}]"
                            ".source_reference_hash"
                        ),
                    ),
                    season=int(seed["season"]),
                    week=int(seed["week"]),
                    status=status,
                    reason=_require_text(
                        seed.get("reason"),
                        f"channels[{position}].seeds[{seed_position}].reason",
                    ),
                    duplicate_of_hash=duplicate_of_hash,
                )
            )
        discovery_limit = int(row.get("discovery_limit", 120))
        if discovery_limit < 1 or discovery_limit > 500:
            raise ValueError("discovery_limit must be between 1 and 500")
        channels.append(
            ChannelConfig(
                key=key,
                provider=_require_text(
                    row.get("provider"), f"channels[{position}].provider"
                ),
                locator=_require_text(
                    row.get("locator"), f"channels[{position}].locator"
                ),
                display_name=_require_text(
                    row.get("display_name"), f"channels[{position}].display_name"
                ),
                program_name=_require_text(
                    row.get("program_name"), f"channels[{position}].program_name"
                ),
                sport=_require_text(
                    row.get("sport"), f"channels[{position}].sport"
                ),
                active=bool(row.get("active", True)),
                season=int(row["season"]),
                expected_speakers=speakers,
                required_patterns=_patterns(
                    row.get("required_patterns", []),
                    f"channels[{position}].required_patterns",
                ),
                candidate_patterns=_patterns(
                    row.get("candidate_patterns", []),
                    f"channels[{position}].candidate_patterns",
                ),
                exclude_patterns=_patterns(
                    row.get("exclude_patterns", []),
                    f"channels[{position}].exclude_patterns",
                ),
                discovery_limit=discovery_limit,
                seeds=tuple(seeds),
            )
        )
    return tuple(channels)


def classify_candidate(
    channel: ChannelConfig,
    *,
    title: str,
    description: str,
) -> CandidateDecision:
    text = f"{title}\n{description}"
    for pattern in channel.exclude_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return CandidateDecision(False, None, None, "matched_exclude_pattern")
    if any(
        not re.search(pattern, text, re.IGNORECASE)
        for pattern in channel.required_patterns
    ):
        return CandidateDecision(False, None, None, "missing_required_pattern")
    if channel.candidate_patterns and not any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in channel.candidate_patterns
    ):
        return CandidateDecision(False, None, None, "missing_pick_pattern")
    week_match = re.search(r"\bweek\s*([0-9]{1,2})\b", text, re.IGNORECASE)
    if week_match is None:
        return CandidateDecision(False, channel.season, None, "week_unresolved")
    week = int(week_match.group(1))
    if week < 1 or week > 22:
        return CandidateDecision(False, channel.season, None, "week_out_of_range")
    return CandidateDecision(True, channel.season, week, "matched_nfl_pick_video")
