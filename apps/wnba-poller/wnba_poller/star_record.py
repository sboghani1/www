"""Deterministic star ratings for resolved leans, and their win/loss record.

A "star" (1-3) is just the leg's pre-game *strength* rendered on a 1-3 scale --
one conviction scale, not a second one to keep in sync. Because strength is a
required, stored field on every revision, the star is a pure function of state:
at resolution ``build_star_grade`` stamps a machine-readable fragment onto the
entry's ``model_lean`` line, e.g.::

    ... | stars: side=1:wrong, total=1:right, fh_total=1:wrong

so nothing depends on remembering to hand-write a rating. ``star-record`` then
tallies right/wrong per tier straight from those fragments.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# strength -> stars. "watch"/"pass" are non-bets (0 = unrated, never tallied).
STRENGTH_STARS = {"strong": 3, "moderate": 2, "small": 1, "watch": 0, "pass": 0}
STAR_TIERS = (1, 2, 3)

_STARS_FRAGMENT = re.compile(r"stars:\s*(.+)$")
_LEG = re.compile(r"([a-z_]+)=([0-3]):(right|wrong|push)")


def strength_to_stars(strength: Any) -> int:
    """Map a leg strength word to a 0-3 star rating (0 = watch/pass/unknown)."""
    return STRENGTH_STARS.get(str(strength or "").strip().lower(), 0)


def build_star_grade(
    active: Mapping[str, Any],
    graded: Mapping[str, Any],
    fh: Mapping[str, Any] | None,
) -> str:
    """Deterministic ``stars: ...`` fragment from stored leg strengths + grades.

    One ``<leg>=<stars>:<result>`` term per graded leg (side/total/fh_side/
    fh_total); returns "" when nothing graded. Written by the resolver, so the
    star record can never silently miss a resolved leg.
    """
    terms: list[str] = []

    def add(leg: str, strength_field: str, graded_leg: Any) -> None:
        if not graded_leg:
            return
        stars = strength_to_stars(active.get(strength_field))
        terms.append(f"{leg}={stars}:{graded_leg['result']}")

    add("side", "full_game_side_strength", graded.get("side"))
    add("total", "full_game_total_strength", graded.get("total"))
    if fh:
        add("fh_side", "first_half_side_strength", fh.get("side"))
        add("fh_total", "first_half_total_strength", fh.get("total"))
    return "stars: " + ", ".join(terms) if terms else ""


def parse_star_legs(model_lean_body: str) -> list[tuple[int, str]]:
    """Return ``(stars, result)`` pairs from a model_lean's ``stars:`` fragment."""
    match = _STARS_FRAGMENT.search(model_lean_body)
    if match is None:
        return []
    return [
        (int(stars), result) for _leg, stars, result in _LEG.findall(match.group(1))
    ]


def _model_lean_bodies(path210_text: str) -> list[str]:
    """Collect ``model_lean:`` bodies from the ``# Past Events`` section only.

    "Notes For Model" documents the schema (with an example fragment) and
    "# Upcoming Events" holds unresolved leans -- neither is a graded result.
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
    "rated_legs": N}`` with each record ``{"right", "wrong", "push"}``. Only
    star tiers 1-3 are tallied (0 = watch/pass, an unrated non-bet).
    """
    tiers: dict[int, dict[str, int]] = {
        tier: {"right": 0, "wrong": 0, "push": 0} for tier in STAR_TIERS
    }
    for body in _model_lean_bodies(path210_text):
        for stars, result in parse_star_legs(body):
            if stars in tiers:
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
        "(right-wrong; star = leg strength, 3=strong/2=moderate/1=small):"
    ]
    labels = {1: "1 (small)", 2: "2 (mod)", 3: "3 (strong)"}
    for tier in STAR_TIERS:
        rows.append(line(labels[tier], record["tiers"][tier]))
    rows.append(line("overall", record["overall"]))
    return "\n".join(rows)
