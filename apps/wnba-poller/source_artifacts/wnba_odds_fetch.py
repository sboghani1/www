#!/usr/bin/env python3
"""
WNBA Odds Fetch
Queries all WNBA games from The Odds API and writes one line per game
(spreads and totals) to a .txt file.

Usage:
    python wnba_odds_fetch.py [output_file] [--log <log_file>]
        Fetch odds, write the latest snapshot to output_file (default
        wnba_lines.txt), and optionally append changed lines plus hourly
        snapshots of unchanged odds to <log_file>.
    python wnba_odds_fetch.py --csv <team>    # print CSV line-movement history
                                              # for one matchup from the log
    python wnba_odds_fetch.py --date-csv YYYY-MM-DD [output_file]
                                              # print histories for one local date
                                              # or safely refresh output_file

Environment:
    ODDS_API_KEY   The Odds API key (loaded from environment or a .env file).

Optional overrides via environment:
    ODDS_REGIONS      default "us2"
    ODDS_MARKETS      default "h2h,spreads,totals"
    ODDS_BOOKMAKERS   default "betonlineag" (set to "" for all books in the region)
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from dotenv import load_dotenv
load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("ODDS_API_KEY", "")
SPORT = "basketball_wnba"
REGIONS = os.environ.get("ODDS_REGIONS", "us2")
MARKETS = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals")
BOOKMAKERS = os.environ.get("ODDS_BOOKMAKERS", "betonlineag")
ODDS_URL = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"


def fetch_odds():
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "american",
    }
    if BOOKMAKERS:
        params["bookmakers"] = BOOKMAKERS

    r = requests.get(ODDS_URL, params=params, timeout=15)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining", "?")
    used = r.headers.get("x-requests-used", "?")
    return r.json(), remaining, used


def format_market(outcomes):
    return ", ".join(
        f"{o['name']} {o.get('point')} ({o['price']:+d})" for o in outcomes
    )


def minutes_until(commence_time, now):
    commence = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    return round((commence - now).total_seconds() / 60)


def format_game(game, now):
    home = game["home_team"]
    away = game["away_team"]
    mins = minutes_until(game["commence_time"], now)
    markets = {
        m["key"]: m["outcomes"]
        for bm in game.get("bookmakers", [])
        for m in bm["markets"]
    }
    spreads = format_market(markets.get("spreads", []))
    totals = format_market(markets.get("totals", []))
    return (
        f"{away} @ {home} | T-{mins}min ({mins / 60:.1f}h) "
        f"| SPREADS: {spreads} | TOTALS: {totals}"
    )


LOG_LINE_RE = re.compile(
    r"^(?P<away>.+?) @ (?P<home>.+?) \| T-(?P<mins>-?\d+)min \((?P<hours>[\d.]+)h\) "
    r"\| SPREADS: (?P<spreads>.*?) \| TOTALS: (?P<totals>.*)$"
)
OUTCOME_RE = re.compile(r"^(?P<name>.+) (?P<point>-?[\d.]+) \((?P<price>[+-]\d+)\)$")
UNCHANGED_LOG_INTERVAL_MINUTES = 60
EVENT_TRACK_MAX_STALE_ENTRIES = 2


def _slug(team):
    return re.sub(r"[^a-z0-9]+", "_", team.lower()).strip("_")


def _parse_outcomes(text):
    out = {}
    for part in text.split(", "):
        om = OUTCOME_RE.match(part)
        if om:
            out[om.group("name")] = (om.group("point"), om.group("price"))
    return out


def _line_signature(line):
    """Return (matchup, value_tuple, minutes_to_tip) for a formatted line.

    value_tuple captures only the odds (spreads + totals), so two lines with the
    same odds but different time-to-tip compare as equal.
    """
    m = LOG_LINE_RE.match(line.strip())
    if not m:
        return None, None, None
    matchup = f"{m.group('away')} @ {m.group('home')}"
    values = tuple(sorted(_parse_outcomes(m.group("spreads")).items())) + tuple(
        sorted(_parse_outcomes(m.group("totals")).items())
    )
    return matchup, values, int(m.group("mins"))


def append_new_lines(lines, log_file):
    """Append changed lines plus hourly snapshots of unchanged odds.

    Returns the number of lines actually appended.
    """
    last = {}
    if os.path.exists(log_file):
        with open(log_file) as f:
            for line in f:
                matchup, values, mins = _line_signature(line)
                if matchup:
                    last[matchup] = (values, mins)

    appended = 0
    with open(log_file, "a") as f:
        for line in lines:
            matchup, values, mins = _line_signature(line)
            if matchup is None:
                continue

            previous = last.get(matchup)
            if previous:
                previous_values, previous_mins = previous
                minutes_since_last = previous_mins - mins
                if (
                    previous_values == values
                    and minutes_since_last < UNCHANGED_LOG_INTERVAL_MINUTES
                ):
                    continue

            f.write(line + "\n")
            last[matchup] = (values, mins)
            appended += 1
    return appended


def _group_event_snapshots(snapshots):
    """Separate repeated matchup entries into individual game histories."""
    tracks = []
    for position, snapshot in enumerate(snapshots):
        minutes = int(snapshot[0].group("mins"))
        candidates = []
        for index, track in enumerate(tracks):
            decrease = track["last_minutes"] - minutes
            stale_entries = position - track["last_seen"]
            if (
                decrease >= 0
                and stale_entries <= EVENT_TRACK_MAX_STALE_ENTRIES
            ):
                candidates.append((decrease, stale_entries, index))

        if candidates:
            _, _, index = min(candidates)
        else:
            tracks.append({
                "snapshots": [],
                "last_minutes": minutes,
                "last_seen": position,
            })
            index = len(tracks) - 1

        tracks[index]["snapshots"].append(snapshot)
        tracks[index]["last_minutes"] = minutes
        tracks[index]["last_seen"] = position

    return [track["snapshots"] for track in tracks]


def matchup_csv(
    team,
    log_file="wnba_lines_log.txt",
    target_minutes=None,
    target_start_minutes=None,
    latest_line=None,
):
    """Build a CSV line-movement history for a single matchup from the log file.

    `team` is any substring of the matchup line (e.g. "Storm" or "Sparks").
    When target times are provided, repeated fixtures are separated and the
    game whose logged time range is closest to those values is selected.
    When `latest_line` is provided, that current snapshot is appended if the
    hourly log does not already contain it.
    Returns the CSV as a string, or "" if no matching lines are found.
    """
    with open(log_file) as f:
        matches = [
            m for line in f
            if team.lower() in line.lower()
            and (m := LOG_LINE_RE.match(line.strip()))
        ]
    if not matches:
        return ""

    snapshots = []
    for m in matches:
        spreads = _parse_outcomes(m.group("spreads"))
        totals = _parse_outcomes(m.group("totals"))
        if not spreads or "Over" not in totals or "Under" not in totals:
            continue
        snapshots.append((m, spreads, totals))
    if not snapshots:
        return ""

    if target_minutes is not None or target_start_minutes is not None:
        event_tracks = []
        by_matchup = {}
        for snapshot in snapshots:
            m = snapshot[0]
            matchup = f"{m.group('away')} @ {m.group('home')}"
            by_matchup.setdefault(matchup, []).append(snapshot)
        for matchup_snapshots in by_matchup.values():
            event_tracks.extend(_group_event_snapshots(matchup_snapshots))
        snapshots = min(
            event_tracks,
            key=lambda track: (
                abs(
                    int(track[0][0].group("mins")) - target_start_minutes
                )
                if target_start_minutes is not None
                else 0,
                abs(
                    int(track[-1][0].group("mins")) - target_minutes
                )
                if target_minutes is not None
                else 0,
            ),
        )

    if latest_line:
        latest_match = LOG_LINE_RE.match(latest_line.strip())
        if latest_match:
            latest_matchup = (
                f"{latest_match.group('away')} @ {latest_match.group('home')}"
            )
            selected_match = snapshots[-1][0]
            selected_matchup = (
                f"{selected_match.group('away')} @ {selected_match.group('home')}"
            )
            latest_spreads = _parse_outcomes(latest_match.group("spreads"))
            latest_totals = _parse_outcomes(latest_match.group("totals"))
            if (
                latest_matchup == selected_matchup
                and latest_spreads
                and "Over" in latest_totals
                and "Under" in latest_totals
            ):
                last_match, last_spreads, last_totals = snapshots[-1]
                if (
                    latest_match.group("mins") != last_match.group("mins")
                    or latest_spreads != last_spreads
                    or latest_totals != last_totals
                ):
                    snapshots.append(
                        (latest_match, latest_spreads, latest_totals)
                    )

    team_order = list(snapshots[0][1].keys())
    header = ["snapshot", "time_to_tip_h"]
    for t in team_order:
        header += [f"{_slug(t)}_spread", f"{_slug(t)}_price"]
    header += ["total", "over_price", "under_price"]

    rows = [",".join(header)]
    for snapshot, (m, spreads, totals) in enumerate(snapshots, start=1):
        over = totals.get("Over", ("", ""))
        under = totals.get("Under", ("", ""))
        values = []
        for t in team_order:
            values += list(spreads.get(t, ("", "")))
        values += [over[0], over[1], under[1]]
        rows.append(",".join([str(snapshot), m.group("hours")] + values))

    return "\n".join(rows)


def _existing_slate_games(export_file):
    """Return matchup and time-range hints from an existing date export."""
    if not export_file or not os.path.exists(export_file):
        return []

    with open(export_file) as f:
        text = f.read().strip()
    if not text:
        return []

    games = []
    for section in re.split(r"\n\s*\n", text):
        lines = section.splitlines()
        if (
            len(lines) < 3
            or " @ " not in lines[0]
            or not lines[1].startswith("snapshot,time_to_tip_h,")
        ):
            continue
        data_rows = [line.split(",") for line in lines[2:] if line.strip()]
        try:
            start_minutes = round(float(data_rows[0][1]) * 60)
            target_minutes = round(float(data_rows[-1][1]) * 60)
        except (IndexError, ValueError):
            continue
        games.append((lines[0], start_minutes, target_minutes))
    return games


def slate_csv(
    target_date,
    latest_file="wnba_lines.txt",
    log_file="wnba_lines_log.txt",
    snapshot_time=None,
    previous_export_file=None,
):
    """Build matchup CSV sections for games on one local calendar date.

    An existing export can seed fixtures that have already started and no
    longer appear in the latest snapshot.
    """
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    if snapshot_time is None:
        snapshot_time = datetime.fromtimestamp(
            os.path.getmtime(latest_file)
        ).astimezone()

    games = []
    by_matchup = {}
    for matchup, start_minutes, target_minutes in _existing_slate_games(
        previous_export_file
    ):
        game = {
            "matchup": matchup,
            "target_start_minutes": start_minutes,
            "target_minutes": target_minutes,
            "latest_line": None,
        }
        games.append(game)
        by_matchup[matchup] = game

    with open(latest_file) as f:
        for line in f:
            m = LOG_LINE_RE.match(line.strip())
            if not m:
                continue
            minutes = int(m.group("mins"))
            if (snapshot_time + timedelta(minutes=minutes)).date() != target_date:
                continue
            matchup = f"{m.group('away')} @ {m.group('home')}"
            game = by_matchup.get(matchup)
            if game is None:
                game = {
                    "matchup": matchup,
                    "target_start_minutes": None,
                    "target_minutes": minutes,
                    "latest_line": line,
                }
                games.append(game)
                by_matchup[matchup] = game
            else:
                game["target_minutes"] = minutes
                game["latest_line"] = line

    sections = []
    for game in games:
        csv = matchup_csv(
            game["matchup"],
            log_file,
            target_minutes=game["target_minutes"],
            target_start_minutes=game["target_start_minutes"],
            latest_line=game["latest_line"],
        )
        if csv:
            sections.append(f"{game['matchup']}\n{csv}")
    return "\n\n".join(sections)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--csv":
        csv = matchup_csv(sys.argv[2])
        if not csv:
            sys.exit(f"No log entries found for matchup containing '{sys.argv[2]}'.")
        print(csv)
        return

    if len(sys.argv) > 2 and sys.argv[1] == "--date-csv":
        output_file = sys.argv[3] if len(sys.argv) > 3 else None
        csv = slate_csv(sys.argv[2], previous_export_file=output_file)
        if not csv:
            sys.exit(f"No games found for {sys.argv[2]}.")
        if output_file:
            with open(output_file, "w") as f:
                f.write(csv + "\n")
            print(f"Wrote {sys.argv[2]} histories to {output_file}")
        else:
            print(csv)
        return

    if not API_KEY:
        sys.exit("ERROR: ODDS_API_KEY is not set (export it or add it to .env).")

    args = sys.argv[1:]
    log_file = None
    if "--log" in args:
        i = args.index("--log")
        log_file = args[i + 1]
        del args[i:i + 2]
    output_file = args[0] if args else "wnba_lines.txt"

    data, remaining, used = fetch_odds()

    now = datetime.now(timezone.utc)
    upcoming = [g for g in data if minutes_until(g["commence_time"], now) > 0]
    lines = [format_game(game, now) for game in upcoming]

    with open(output_file, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    print(f"Wrote {len(upcoming)} upcoming WNBA game(s) to {output_file} "
          f"({len(data) - len(upcoming)} already started, skipped)")
    if log_file:
        appended = append_new_lines(lines, log_file)
        print(f"Appended {appended} log line(s) to {log_file} "
              f"({len(lines) - appended} unchanged within one hour, skipped)")
    print(f"Quota: {used} used, {remaining} remaining")


if __name__ == "__main__":
    main()
