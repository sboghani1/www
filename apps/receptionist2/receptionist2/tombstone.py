"""Tombstones: hand a chat's work to a fresh chat through a written file."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

SKILL = "www/apps/receptionist2/skills/receptionist2-tombstone/SKILL.md"
SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_TRAILING_GENERATION = re.compile(rf"^(.*?) ([{SUPERSCRIPTS}]+)$")


def tombstone_path(directory: Path, session_id: str, now: datetime) -> Path:
    return directory / f"{session_id[:8]}-{now:%Y%m%d-%H%M%S}.md"


def write_prompt(path: Path, chat_name: str) -> str:
    return (
        f"Read and follow `{SKILL}`.\n\n"
        f"Write the tombstone for this chat ({chat_name}).\n"
        f"TOMBSTONE_PATH={path}"
    )


def resume_prompt(path: Path) -> str:
    return (
        f"Read and follow `{SKILL}`.\n\n"
        "Continue from the tombstone of the previous chat.\n"
        f"TOMBSTONE_PATH={path}"
    )


def continuation_name(name: str) -> str:
    """'Packers ATS' -> 'Packers ATS ²', 'Packers ATS ²' -> 'Packers ATS ³'."""
    match = _TRAILING_GENERATION.match(name)
    if match:
        base = match.group(1)
        generation = int(match.group(2).translate(str.maketrans(SUPERSCRIPTS, "0123456789")))
    else:
        base, generation = name, 1
    following = str(generation + 1).translate(str.maketrans("0123456789", SUPERSCRIPTS))
    return f"{base} {following}"
