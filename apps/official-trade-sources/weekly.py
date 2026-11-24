"""Refresh current identities and an independent weekly discrepancy list."""

import json
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET

from archive_live import encode, fetch, get, put

HOUSE = "https://clerk.house.gov/xml/lists/MemberData.xml"
SENATE = "https://www.senate.gov/general/contact_information/senators_cfm.xml"
SIGNALS = "https://capitolsignals.org"
SCHEMA = """
CREATE TABLE IF NOT EXISTS member(bio TEXT PRIMARY KEY, chamber TEXT, seat TEXT, name TEXT, short_name TEXT);
CREATE TABLE IF NOT EXISTS weekly_gap(week TEXT, source_id TEXT, body TEXT, PRIMARY KEY(week,source_id,body));
"""


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode().lower().replace("'", "").replace("-", "")))


def rosters(db: sqlite3.Connection, week: str) -> int:
    if get(db, "rosters_week") != week:
        members = []
        for m in ET.fromstring(fetch(HOUSE)).findall("members/member"):
            bio = m.findtext("member-info/bioguideID")
            if bio:
                first, last = (m.findtext("member-info/" + k) for k in ("firstname", "lastname"))
                seat = m.findtext("statedistrict")
                members.append((bio, "house", seat, m.findtext("member-info/official-name"), f"{first} {last}"))
        house_count = len(members)
        for m in ET.fromstring(fetch(SENATE)).findall("member"):
            first, last = (m.findtext(k) for k in ("first_name", "last_name"))
            members.append((m.findtext("bioguide_id"), "senate", m.findtext("state"), f"{first} {last}",
                            f"{first.split()[0]} {last}"))
        if house_count < 350 or len(members) - house_count < 80 or any(not all(m) for m in members):
            raise ValueError("Incomplete official roster; keeping prior membership")
        with db:
            db.execute("DELETE FROM member")
            db.executemany("INSERT INTO member VALUES(?,?,?,?,?)", members)
            put(db, "rosters_week", week)
    return db.execute("SELECT count(*) FROM member").fetchone()[0]


def signals(db: sqlite3.Connection, week: str) -> dict:
    if get(db, "signals_week") == week:
        return {"status": "already compared", "week": week}
    page = fetch(SIGNALS + "/data/").decode()
    weeks = re.findall(r'href="/data/(\d{4}-W\d{2})/trades\.jsonl"', page)
    if not weeks or max(weeks) != week:
        raise ValueError(f"Weekly release for {week} not posted; latest {max(weeks, default='unknown')}")
    if not get(db, "feed_checked"):
        raise ValueError("Cannot compare weekly data without a baseline")
    keys = set(db.execute("""SELECT json_extract(body,'$.memberId'),json_extract(body,'$.transactionDate'),
           upper(json_extract(body,'$.ticker')) FROM feed WHERE tbl='political_trade_events'
           AND json_extract(body,'$.supersededAt') IS NULL"""))
    source = f"{SIGNALS}/data/{week}/trades.jsonl"
    lines = fetch(source, 64 << 20).splitlines()
    if not lines:
        raise ValueError("Empty weekly release")
    with db:
        for line in lines:
            row = json.loads(line)
            key = row["bioguide_id"], row["transaction_date"], (row["ticker"] or "").upper()
            if key not in keys:
                db.execute("INSERT OR IGNORE INTO weekly_gap VALUES(?,?,?)", (week, row["trade_id"], encode(row)))
        put(db, "signals_week", week)
    return {"week": week, "rows": len(lines), "unmatched_leads": db.execute(
        "SELECT count(*) FROM weekly_gap WHERE week=?", (week,)).fetchone()[0], "source": source}
