from __future__ import annotations

from typing import Any, Mapping

Result = str  # "right" | "wrong" | "push"


def grade_side(
    *,
    away_team: str,
    home_team: str,
    away_score: int,
    home_score: int,
    selection: str,
    line: float,
) -> Result:
    """Grade a full-game side pick.

    `line` is the spread for the *selected* team (negative when favored,
    positive when the underdog) -- i.e. `latest_away_spread` if `selection`
    is the away team, `latest_home_spread` if it is the home team.
    """
    if selection == away_team:
        margin = (away_score - home_score) + line
    elif selection == home_team:
        margin = (home_score - away_score) + line
    else:
        raise ValueError("side selection does not match either team")
    if margin > 0:
        return "right"
    if margin < 0:
        return "wrong"
    return "push"


def grade_total(
    *,
    away_score: int,
    home_score: int,
    selection: str,
    line: float,
) -> Result:
    if selection not in {"Over", "Under"}:
        raise ValueError("total selection must be Over or Under")
    actual = away_score + home_score
    if actual == line:
        return "push"
    over_hit = actual > line
    if selection == "Over":
        return "right" if over_hit else "wrong"
    return "wrong" if over_hit else "right"


def _numeric_line(value: Any) -> float:
    if value in (None, "", "nodata"):
        raise ValueError("no closing line is available to grade against")
    return float(value)


def grade_lean(
    *,
    game: Mapping[str, Any],
    active_revision: Mapping[str, Any],
) -> dict[str, Any]:
    """Grade every leg of the active revision's full-game lean against the
    game's recorded final score and closing lines.

    Both scores must already be recorded on `game` (away_score/home_score);
    callers are expected to resolve those from the Sheet, not invent them.
    First-half legs are deliberately not graded -- the same "no invented
    market/state" boundary that keeps generation from inventing a
    first-half lean when evidence is weak also keeps grading from
    inventing a first-half score this poller never tracks.
    """
    away_team = str(game.get("away_team") or "")
    home_team = str(game.get("home_team") or "")
    away_score_raw = game.get("away_score")
    home_score_raw = game.get("home_score")
    if away_score_raw in (None, "") or home_score_raw in (None, ""):
        raise ValueError("game has no recorded final score")
    away_score = int(away_score_raw)
    home_score = int(home_score_raw)

    result: dict[str, Any] = {
        "away_team": away_team,
        "home_team": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "side": None,
        "total": None,
    }

    side_selection = str(active_revision.get("full_game_side_selection") or "")
    if side_selection:
        line_field = (
            "latest_away_spread"
            if side_selection == away_team
            else "latest_home_spread"
        )
        line = _numeric_line(game.get(line_field))
        result["side"] = {
            "selection": side_selection,
            "line": line,
            "result": grade_side(
                away_team=away_team,
                home_team=home_team,
                away_score=away_score,
                home_score=home_score,
                selection=side_selection,
                line=line,
            ),
        }

    total_selection = str(active_revision.get("full_game_total_selection") or "")
    if total_selection:
        line = _numeric_line(game.get("latest_total"))
        result["total"] = {
            "selection": total_selection,
            "line": line,
            "result": grade_total(
                away_score=away_score,
                home_score=home_score,
                selection=total_selection,
                line=line,
            ),
        }

    return result


def period_score(
    result_row: Mapping[str, Any], side: str, through_quarter: int
) -> int | None:
    """Sum a team's quarter points through `through_quarter` (1=Q1, 2=H1),
    or None if any needed quarter is missing.
    """
    total = 0
    for quarter in range(1, through_quarter + 1):
        raw = result_row.get(f"{side}_q{quarter}")
        if raw in (None, "", "nodata"):
            return None
        try:
            total += int(raw)
        except (TypeError, ValueError):
            return None
    return total


def grade_first_half_lean(
    *,
    game: Mapping[str, Any],
    active_revision: Mapping[str, Any],
    result_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Grade first-half side/total legs from stored Q1+Q2 quarter scores in
    `result_row` and the closing first-half lines on `game`. Returns
    ``{"away_h1", "home_h1", "side", "total"}`` with ``None`` legs when the
    lean has no first-half selection or quarter scores are unavailable.
    """
    away_team = str(game.get("away_team") or "")
    home_team = str(game.get("home_team") or "")
    away_h1 = period_score(result_row, "away", 2)
    home_h1 = period_score(result_row, "home", 2)
    out: dict[str, Any] = {
        "away_h1": away_h1,
        "home_h1": home_h1,
        "side": None,
        "total": None,
    }
    if away_h1 is None or home_h1 is None:
        return out

    side_selection = str(active_revision.get("first_half_side_selection") or "")
    if side_selection:
        line_field = (
            "latest_first_half_away_spread"
            if side_selection == away_team
            else "latest_first_half_home_spread"
        )
        line = _numeric_line(game.get(line_field))
        out["side"] = {
            "selection": side_selection,
            "line": line,
            "result": grade_side(
                away_team=away_team,
                home_team=home_team,
                away_score=away_h1,
                home_score=home_h1,
                selection=side_selection,
                line=line,
            ),
        }

    total_selection = str(
        active_revision.get("first_half_total_selection") or ""
    )
    if total_selection:
        line = _numeric_line(game.get("latest_first_half_total"))
        out["total"] = {
            "selection": total_selection,
            "line": line,
            "result": grade_total(
                away_score=away_h1,
                home_score=home_h1,
                selection=total_selection,
                line=line,
            ),
        }

    return out
