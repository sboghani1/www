"""Star ratings for leans, and their win/loss record.

A "star" (1-3) is a per-leg pre-game CONVICTION set at generation, deliberately
independent of *strength* (stake/aggressiveness) -- a leg can be sized "small"
yet carry a 2-star signal (e.g. a convicted fade of a firm line). To keep it
deterministic without a Sheet-schema change, generation embeds the stars in the
revision's stored ``summary`` as a ``[stars: side=2, total=1, fh_total=2]``
token; at resolution ``build_star_grade`` reads them back from that stored state
(falling back to strength for pre-feature leans) and stamps a machine-readable
``... | stars: side=2:wrong, total=1:right`` fragment onto the entry's
``model_lean``. ``star-record`` then tallies right/wrong per tier from those
fragments -- capture never depends on remembering to hand-write anything.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Fallback only: strength -> stars for leans made before stars were captured.
STRENGTH_STARS = {"strong": 3, "moderate": 2, "small": 1, "watch": 0, "pass": 0}
STAR_TIERS = (1, 2, 3)
STAR_LEGS = ("side", "total", "fh_side", "fh_total")
_STRENGTH_FIELD = {
    "side": "full_game_side_strength",
    "total": "full_game_total_strength",
    "fh_side": "first_half_side_strength",
    "fh_total": "first_half_total_strength",
}

_STARS_FRAGMENT = re.compile(r"stars:\s*(.+)$")
_LEG = re.compile(r"([a-z_]+)=([0-3]):(right|wrong|push)")
_SUMMARY_TOKEN = re.compile(r"\[stars:\s*([^\]]*)\]")
_SUMMARY_LEG = re.compile(r"([a-z_]+)=([1-3])")


def strength_to_stars(strength: Any) -> int:
    """Map a leg strength word to a 0-3 star rating (0 = watch/pass/unknown)."""
    return STRENGTH_STARS.get(str(strength or "").strip().lower(), 0)


def format_stars_token(stars: Mapping[str, int]) -> str:
    """Render the ``[stars: ...]`` token embedded in a revision summary."""
    terms = [f"{leg}={stars[leg]}" for leg in STAR_LEGS if leg in stars]
    return f"[stars: {', '.join(terms)}]" if terms else ""


def stars_from_summary(summary: str) -> dict[str, int]:
    """Parse the ``[stars: ...]`` token from a stored summary, if present."""
    match = _SUMMARY_TOKEN.search(summary or "")
    if match is None:
        return {}
    return {leg: int(val) for leg, val in _SUMMARY_LEG.findall(match.group(1))}


def strip_stars_token(summary: str) -> str:
    """Remove any ``[stars: ...]`` token so a fresh one can be embedded."""
    return _SUMMARY_TOKEN.sub("", summary or "").strip()


def build_star_grade(
    active: Mapping[str, Any],
    graded: Mapping[str, Any],
    fh: Mapping[str, Any] | None,
) -> str:
    """``stars: ...`` fragment for model_lean from the generation-time stars.

    Prefers the per-leg conviction stored in the revision summary token; falls
    back to strength for leans made before stars were captured. One
    ``<leg>=<stars>:<result>`` term per graded leg; "" when nothing graded.
    """
    summary_stars = stars_from_summary(str(active.get("summary") or ""))

    def leg_stars(leg: str) -> int:
        if leg in summary_stars:
            return summary_stars[leg]
        return strength_to_stars(active.get(_STRENGTH_FIELD[leg]))

    terms: list[str] = []

    def add(leg: str, graded_leg: Any) -> None:
        if graded_leg:
            terms.append(f"{leg}={leg_stars(leg)}:{graded_leg['result']}")

    add("side", graded.get("side"))
    add("total", graded.get("total"))
    if fh:
        add("fh_side", fh.get("side"))
        add("fh_total", fh.get("total"))
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
        "(right-wrong; star = per-leg conviction, 3=top/2=real read/1=lean):"
    ]
    labels = {1: "1 (lean)", 2: "2 (read)", 3: "3 (top)"}
    for tier in STAR_TIERS:
        rows.append(line(labels[tier], record["tiers"][tier]))
    rows.append(line("overall", record["overall"]))
    return "\n".join(rows)
