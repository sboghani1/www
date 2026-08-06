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
