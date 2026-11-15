#!/usr/bin/env python3
"""Capture Bovada live lines at the end of Q1 and at halftime of NBA games.

Game state comes from ESPN's public scoreboard (one call covers the slate).
Bovada (free; the paid Odds API is never used) is read:
- once per tip slot, about 10 minutes before tip: the pre-match line;
- while a game sits in its end-of-Q1 break or at halftime: MAX_PER_BREAK
  captures per game per break, the second SECOND_CAPTURE_AFTER after the
  first. Bovada's first live line after a break is sometimes still the
  previous quarter's, so analysis should use the LAST capture of a break;
- capped at MAX_BOVADA_CALLS per night.
One Bovada read serves every game due at that moment.

Output (<data-dir>/<league>/<YYYYMMDD>/):
  pregame.jsonl    one row per game: Bovada pre-match markets ~10 min before tip
  captures.jsonl   one row per game per capture: ESPN state + parsed live markets
  raw/bovada_<utc>.json.gz   the NBA block of each Bovada response
  poll.log, state.json (resume-safe)

Usage:
  track_live.py [--date YYYYMMDD] [--data-dir DIR]   run until every game is past halftime
  track_live.py --self-test                          one ESPN + one pre-match Bovada read
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

LEAGUES = {
    "nba": {
        "espn": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        # The league path (/basketball/nba) answers a CDN-cached empty body in
        # every encoding; the sport feed fetched with gzip is fresh, so read it
        # and keep the /basketball/nba block (checked 2026-11-14).
        "bovada": "https://www.bovada.lv/services/sports/event/v2/events/A/description/basketball",
        "bovada_link": "/basketball/nba",
    },
}

MAX_PER_BREAK = 2
SECOND_CAPTURE_AFTER = {"q1": 60, "half": 300}  # seconds after the first capture
PREGAME_LEAD = 10 * 60  # read the pre-match line this long before tip
MAX_BOVADA_CALLS = 90
_MAIN_MARKETS = {"moneyline", "point spread", "total"}


def log(path: Path, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with path.open("a") as f:
        f.write(line + "\n")


def get_json(url: str, params: dict[str, str]):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip, deflate"}
    req = urllib.request.Request(f"{url}?{query}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    return json.loads(raw or b"null")


def bovada_fetch(league: str, live_only: bool | None):
    """(events in the league block, the league block as JSON bytes).

    live_only None = no filter (pre-match and live together)."""
    params = {"lang": "en"}
    if live_only is not None:
        params["liveOnly" if live_only else "preMatchOnly"] = "true"
    data = get_json(LEAGUES[league]["bovada"], params)
    blocks = [
        b for b in (data if isinstance(data, list) else [])
        if isinstance(b, dict)
        and ((b.get("path") or [{}])[0].get("link") or "") == LEAGUES[league]["bovada_link"]
    ]
    return [e for b in blocks for e in b.get("events") or []], json.dumps(blocks).encode()


def clock_seconds(display: str) -> float:
    try:
        if ":" in display:
            m, s = display.split(":", 1)
            return int(m) * 60 + float(s)
        return float(display)
    except ValueError:
        return 0.0


def parse_espn(data: dict) -> list[dict]:
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        st = ev["status"]
        teams = {}
        for c in comp["competitors"]:
            teams[c["homeAway"]] = {
                "name": c["team"]["displayName"],
                "nickname": c["team"].get("name") or c["team"]["displayName"].split()[-1],
                "abbr": c["team"].get("abbreviation"),
                "score": int(float(c.get("score") or 0)),
                "linescores": [ls.get("value") for ls in c.get("linescores") or []],
            }
        odds = (comp.get("odds") or [{}])[0]
        games.append({
            "espn_id": ev["id"],
            "tip_utc": ev["date"],
            "short": ev.get("shortName"),
            "state": st["type"]["state"],
            "status_name": st["type"].get("name"),
            "detail": st["type"].get("shortDetail") or st["type"].get("detail"),
            "period": st.get("period") or 0,
            "clock": st.get("displayClock") or "",
            "home": teams.get("home"),
            "away": teams.get("away"),
            "espn_odds": {
                "details": odds.get("details"),
                "spread": odds.get("spread"),
                "over_under": odds.get("overUnder"),
                "provider": (odds.get("provider") or {}).get("name"),
            },
        })
    return games


def espn_games(league: str, date: str) -> list[dict]:
    return parse_espn(get_json(LEAGUES[league]["espn"], {"dates": date}))


def break_kind(g: dict) -> str | None:
    """'q1' / 'half' while the game sits in that break, else None."""
    if g["state"] != "in":
        return None
    name = (g["status_name"] or "").upper()
    detail = (g["detail"] or "").lower()
    if name == "STATUS_HALFTIME" or detail.startswith("halftime"):
        return "half"
    at_zero = clock_seconds(g["clock"]) == 0
    if g["period"] == 1 and (name == "STATUS_END_PERIOD" or "end of 1st" in detail or at_zero):
        return "q1"
    if g["period"] == 2 and (name == "STATUS_END_PERIOD" or "end of 2nd" in detail or at_zero):
        return "half"
    return None


def missed_break(g: dict) -> str | None:
    """Break we never saw because the next period already started (first minute)."""
    if g["state"] != "in":
        return None
    if g["period"] == 2 and clock_seconds(g["clock"]) >= 660:
        return "q1"
    if g["period"] == 3 and clock_seconds(g["clock"]) >= 660:
        return "half"
    return None


def due_capture(g: dict, caps: dict, now: float) -> tuple[str, bool] | None:
    """(checkpoint, late) if this game needs a Bovada capture right now."""
    kind = break_kind(g)
    if kind:
        times = caps.get(kind, [])
        if not times:
            return kind, False
        if len(times) < MAX_PER_BREAK and now - times[-1] >= SECOND_CAPTURE_AFTER[kind]:
            return kind, False
        return None
    late = missed_break(g)
    if late and not caps.get(late):
        return late, True
    return None


def still_pending(g: dict, caps: dict) -> bool:
    """Keep polling for this game?"""
    if g["state"] == "pre":
        return True
    if g["state"] != "in":
        return False
    if g["period"] <= 2:
        # Through Q2 and the whole halftime break (status period stays 2), so
        # the 2nd halftime capture is never skipped.
        return len(caps.get("half", [])) < MAX_PER_BREAK or break_kind(g) is None
    # Early Q3: only to grab a late halftime capture we never got.
    return g["period"] == 3 and not caps.get("half") and missed_break(g) == "half"


def parse_markets(event: dict) -> list[dict]:
    out = []
    for group in event.get("displayGroups") or []:
        for m in group.get("markets") or []:
            desc = str(m.get("description") or "").lower()
            if desc not in _MAIN_MARKETS:
                continue
            out.append({
                "group": group.get("description"),
                "market": desc,
                "period": (m.get("period") or {}).get("abbreviation"),
                "period_desc": (m.get("period") or {}).get("description"),
                "status": m.get("status"),
                "outcomes": [
                    {
                        "desc": o.get("description"),
                        "type": o.get("type"),
                        "status": o.get("status"),
                        "american": (o.get("price") or {}).get("american"),
                        "handicap": (o.get("price") or {}).get("handicap"),
                    }
                    for o in m.get("outcomes") or []
                ],
            })
    return out


def full_game_line(markets: list[dict]) -> tuple:
    """(spread handicaps, moneylines) of the full-game market — staleness check."""
    sp = next((m for m in markets if m["market"] == "point spread" and m["period"] == "G"), None)
    ml = next((m for m in markets if m["market"] == "moneyline" and m["period"] == "G"), None)
    return (
        tuple(o["handicap"] for o in sp["outcomes"]) if sp else None,
        tuple(o["american"] for o in ml["outcomes"]) if ml else None,
    )


def match_event(g: dict, events: list[dict]) -> dict | None:
    hits = [
        e for e in events
        if g["home"]["nickname"].lower() in str(e.get("description", "")).lower()
        and g["away"]["nickname"].lower() in str(e.get("description", "")).lower()
    ]
    return hits[0] if len(hits) == 1 else None


def tip_ts(g: dict) -> float:
    return datetime.fromisoformat(g["tip_utc"].replace("Z", "+00:00")).timestamp()


def interval(games: list[dict], state: dict, now: float) -> float:
    """Seconds until the next poll: tight only near a break we still need."""
    waits = []
    for g in games:
        caps = state["captures"].get(g["espn_id"], {})
        if g["state"] == "pre":
            to_tip = tip_ts(g) - now
            if g["espn_id"] not in state["pregame"] and to_tip > 60:
                waits.append(max(30.0, to_tip - PREGAME_LEAD))
            else:
                # First quarter takes ~25 min of real time; no need to look before then.
                waits.append(max(60.0, to_tip + 20 * 60))
        elif still_pending(g, caps):
            kind = break_kind(g)
            c = clock_seconds(g["clock"])
            if kind:
                times = caps.get(kind, [])
                left = SECOND_CAPTURE_AFTER[kind] - (now - times[-1]) if times else 0
                waits.append(max(30.0, left))
            elif g["period"] >= 3 or c <= 90:
                waits.append(30.0)
            elif c <= 240:
                waits.append(60.0)
            else:
                waits.append(150.0)
    return min(waits) if waits else 600.0


def run(league: str, date: str, data_dir: Path) -> int:
    out = data_dir / league / date
    (out / "raw").mkdir(parents=True, exist_ok=True)
    logf = out / "poll.log"
    state_path = out / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("captures", {})
    state.setdefault("pregame", {})
    state.setdefault("espn_pregame", {})
    state.setdefault("last_line", {})
    state.setdefault("bovada_calls", 0)
    log(logf, f"start league={league} date={date} bovada_calls={state['bovada_calls']}")

    def save():
        state_path.write_text(json.dumps(state))

    def bovada(live_only, why):
        if state["bovada_calls"] >= MAX_BOVADA_CALLS:
            log(logf, f"bovada cap reached ({MAX_BOVADA_CALLS}); skipped {why}")
            return None, b""
        state["bovada_calls"] += 1
        try:
            return bovada_fetch(league, live_only)
        except Exception as exc:  # noqa: BLE001 — network blip: next poll retries
            log(logf, f"bovada error ({why}) {exc!r}")
            return None, b""

    first = True
    while True:
        try:
            games = espn_games(league, date)
        except Exception as exc:  # noqa: BLE001
            log(logf, f"espn error {exc!r}")
            time.sleep(60)
            continue
        if first and not games:
            log(logf, "no games on this date; exiting")
            return 0
        first = False
        now = time.time()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # ESPN drops its odds once a game starts: keep the last pre-tip copy.
        for g in games:
            if g["state"] == "pre" and g["espn_odds"].get("details"):
                state["espn_pregame"][g["espn_id"]] = g["espn_odds"]

        # Pre-match Bovada line, once per game, inside PREGAME_LEAD of tip.
        pre_due = [
            g for g in games
            if g["state"] == "pre" and g["espn_id"] not in state["pregame"]
            and tip_ts(g) - now <= PREGAME_LEAD + 30
        ]
        if pre_due:
            events, raw = bovada(False, "pregame")
            if events is not None:
                if raw:
                    (out / "raw" / f"bovada_pre_{stamp}.json.gz").write_bytes(gzip.compress(raw))
                for g in pre_due:
                    ev = match_event(g, events)
                    row = {
                        "captured_utc": stamp, "checkpoint": "pregame",
                        "espn": g, "bovada_event_id": ev.get("id") if ev else None,
                        "markets": parse_markets(ev) if ev else [],
                    }
                    with (out / "pregame.jsonl").open("a") as f:
                        f.write(json.dumps(row) + "\n")
                    state["pregame"][g["espn_id"]] = stamp
                    log(logf, f"pregame {g['short']} markets={len(row['markets'])} matched={ev is not None}")
                save()

        due = []
        for g in games:
            caps = state["captures"].setdefault(g["espn_id"], {})
            d = due_capture(g, caps, now)
            if d:
                due.append((g, *d))
        if due:
            events, raw = bovada(True, "live")
            if events is not None and any(match_event(g, events) is None for g, _, _ in due):
                more, raw2 = bovada(None, "live fallback")
                if more is not None:
                    seen = {e.get("id") for e in events}
                    events += [e for e in more if e.get("id") not in seen]
                    raw = raw + b"\n" + raw2
            if events is not None:
                if raw:
                    (out / "raw" / f"bovada_{stamp}.json.gz").write_bytes(gzip.compress(raw))
                for g, k, late in due:
                    ev = match_event(g, events)
                    markets = parse_markets(ev) if ev else []
                    line = full_game_line(markets)
                    prev = state["last_line"].get(g["espn_id"])
                    prev_k = prev["checkpoint"] if prev else None
                    row = {
                        "captured_utc": stamp,
                        "checkpoint": k,
                        "capture_no": len(state["captures"][g["espn_id"]].get(k, [])) + 1,
                        "late": late,
                        # Same full-game line as the previous checkpoint after the
                        # score moved = Bovada had not re-priced yet.
                        "same_line_as_previous_checkpoint": bool(
                            markets and prev and prev_k != k and list(line) == prev["line"]
                        ),
                        "espn": g,
                        "espn_pregame_odds": state["espn_pregame"].get(g["espn_id"]),
                        "bovada_event_id": ev.get("id") if ev else None,
                        "bovada_live": ev.get("live") if ev else None,
                        "markets": markets,
                    }
                    with (out / "captures.jsonl").open("a") as f:
                        f.write(json.dumps(row) + "\n")
                    # A capture with no markets doesn't use up the break.
                    if markets:
                        state["captures"][g["espn_id"]].setdefault(k, []).append(now)
                        state["last_line"][g["espn_id"]] = {"checkpoint": k, "line": list(line)}
                    n_open = sum(1 for m in markets if m["status"] in (None, "O"))
                    log(logf, f"capture {g['short']} {k}#{row['capture_no']}{' LATE' if late else ''} "
                              f"{g['away']['score']}-{g['home']['score']} p{g['period']} {g['clock']} "
                              f"markets={len(markets)} open={n_open} matched={ev is not None}"
                              f"{' SAME-LINE' if row['same_line_as_previous_checkpoint'] else ''}")
            save()

        pending = [g for g in games if still_pending(g, state["captures"].get(g["espn_id"], {}))]
        if not pending:
            log(logf, f"done: every game past halftime or final; bovada_calls={state['bovada_calls']}")
            return 0
        wait = interval(games, state, time.time())
        live = [f"{g['short']} p{g['period']} {g['clock']}" for g in games if g["state"] == "in"]
        log(logf, f"poll ok live=[{'; '.join(live)}] next in {int(wait)}s bovada_calls={state['bovada_calls']}")
        time.sleep(wait)


def self_test(league: str, date: str) -> int:
    games = espn_games(league, date)
    events, _ = bovada_fetch(league, live_only=False)
    print(f"espn games {len(games)}, bovada pre-match events {len(events)}")
    for g in games:
        ev = match_event(g, events)
        mk = parse_markets(ev) if ev else []
        print(f"- {g['short']} {g['state']} tip {g['tip_utc']} matched={ev is not None} markets={len(mk)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="nba", choices=sorted(LEAGUES))
    ap.add_argument("--date", default=datetime.now(ET).strftime("%Y%m%d"))
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    return self_test(a.league, a.date) if a.self_test else run(a.league, a.date, a.data_dir)


if __name__ == "__main__":
    sys.exit(main())
