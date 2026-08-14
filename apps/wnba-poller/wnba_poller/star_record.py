"""Tally the win/loss record of star-rated legs from resolved path210 leans.

Star ratings (1-3 per leg) are not a Sheet field -- they live in each resolved
entry's ``model_lean:`` line as a run of ``*`` stars written at resolution time
(see the ``model_lean`` convention in path210's "Notes For Model"). A leg looks
like::

    side (** small TORONTO TEMPO +10, ...) -- HIT (tempo covered)

Legs are separated by ``;``; each carries a ``HIT``/``MISS`` (or ``PUSH``)
verdict already used elsewhere in the line. This module reads only the stars and
the verdict, so it never has to re-grade anything -- it just cross-tabulates
"how have my N-star calls actually done."
"""

from __future__ import annotations

import re
from typing import Any

# Accept both the plain "*" the resolve flow writes and the unicode stars a
# human might paste; count a run of them as the leg's rating.
_STAR_RUN = re.compile(r"(?:⭐|★|\*){1,3}")
# Anchor the verdict to the "-- HIT" marker the resolve flow writes per leg, so
# a trailing "net: TRIPLE HIT ..." summary can't be read as a second verdict.
_VERDICT = re.compile(r"--\s*(HIT|MISS|PUSH|RIGHT|WRONG)\b", re.IGNORECASE)
_VERDICT_MAP = {
    "HIT": "right",
    "RIGHT": "right",
    "MISS": "wrong",
    "WRONG": "wrong",
    "PUSH": "push",
}

STAR_TIERS = (1, 2, 3)


def leg_stars(segment: str) -> int | None:
    """Return the 1-3 star rating of a model_lean leg, or None if unrated.

    Uses the longest star run in the segment (so a stray ``*`` elsewhere does
    not undercount an explicit ``***``), capped at 3.
    """
    runs = _STAR_RUN.findall(segment)
    if not runs:
        return None
    stars = max(len(re.sub(r"[^⭐★*]", "", run)) for run in runs)
    return stars if 1 <= stars <= 3 else None


def leg_result(segment: str) -> str | None:
    """Return "right"/"wrong"/"push" from the leg's "-- HIT/MISS/PUSH" marker."""
    match = _VERDICT.search(segment)
    if match is None:
        return None
    return _VERDICT_MAP[match.group(1).upper()]


def _model_lean_bodies(path210_text: str) -> list[str]:
    """Collect ``model_lean:`` bodies from the ``# Past Events`` section only.

    The "Notes For Model" section documents the model_lean schema (including a
    starred example), and "# Upcoming Events" holds unresolved leans -- neither
    is a graded result, so only real resolved entries are read.
    """
    bodies = []
    in_past_events = False
    for line in path210_text.splitlines():
        if line.startswith("# "):
            in_past_events = line.strip() == "# Past Events"
            continue
        if in_past_events and line.startswith("model_lean:"):
            bodies.append(line[len("model_lean:") :])
    return bodies


def compute_star_record(path210_text: str) -> dict[str, Any]:
    """Cross-tabulate star rating vs. outcome across every resolved leg.

    Returns ``{"tiers": {1: {...}, 2: {...}, 3: {...}}, "overall": {...},
    "rated_legs": N}`` where each record is ``{"right", "wrong", "push"}``.
    Only legs that carry both a star run and a verdict are counted.
    """
    tiers: dict[int, dict[str, int]] = {
        tier: {"right": 0, "wrong": 0, "push": 0} for tier in STAR_TIERS
    }
    for body in _model_lean_bodies(path210_text):
        for segment in body.split(";"):
            stars = leg_stars(segment)
            result = leg_result(segment)
            if stars is None or result is None:
                continue
            tiers[stars][result] += 1

    overall = {"right": 0, "wrong": 0, "push": 0}
    for record in tiers.values():
        for key in overall:
            overall[key] += record[key]
    rated = sum(sum(record.values()) for record in tiers.values())
    return {"tiers": tiers, "overall": overall, "rated_legs": rated}


def format_star_record(record: dict[str, Any]) -> str:
    """Render the record as a compact fixed-width table."""

    def line(label: str, rec: dict[str, int]) -> str:
        decided = rec["right"] + rec["wrong"]
        pct = f"{100 * rec['right'] / decided:.0f}%" if decided else "  -"
        push = f" +{rec['push']}P" if rec["push"] else ""
        return (
            f"  {label:<9} {rec['right']}-{rec['wrong']}{push:<4}  "
            f"({pct} of decided)"
        )

    rows = [
        f"Star record over {record['rated_legs']} rated legs "
        "(right-wrong, from resolved model_lean stars):"
    ]
    stars = {1: "*", 2: "**", 3: "***"}
    for tier in STAR_TIERS:
        rows.append(line(stars[tier], record["tiers"][tier]))
    rows.append(line("overall", record["overall"]))
    return "\n".join(rows)
