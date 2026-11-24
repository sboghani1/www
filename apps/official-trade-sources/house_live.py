"""Keep index observations, PDF evidence and unresolved rows separate."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from archive_live import encode, get, now, put
from monitor import download
from report_rows import Entry, house_report
from weekly import normalized

FORM_URL = r"https://disclosures-clerk\.house\.gov/public_disc/(ptr-pdfs|financial-pdfs)/(\d{4})/(\d+)\.pdf"
SCHEMA = """
CREATE TABLE IF NOT EXISTS house_doc(url TEXT PRIMARY KEY, metadata TEXT NOT NULL, first_seen TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, sha TEXT);
CREATE TABLE IF NOT EXISTS filing_change(seq INTEGER PRIMARY KEY, seen TEXT, url TEXT, kind TEXT,
 old_metadata TEXT, new_metadata TEXT);
CREATE TABLE IF NOT EXISTS original(url TEXT,sha TEXT,seen TEXT,bio TEXT,reason TEXT,issues TEXT,
 PRIMARY KEY(url,sha));
CREATE TABLE IF NOT EXISTS original_row(url TEXT,sha TEXT,ordinal INT,bio TEXT,tier TEXT,
 trade_date TEXT,asset TEXT,amount_low REAL,owner TEXT,page INT,body TEXT,PRIMARY KEY(url,sha,ordinal));
CREATE INDEX IF NOT EXISTS original_match ON original_row(bio,trade_date);
CREATE VIEW IF NOT EXISTS verified_trade AS SELECT r.*,json_extract(d.metadata,'$.filer') AS filer,
 json_extract(d.metadata,'$.filed_on') AS filed_on,d.first_seen,o.reason AS observation
 FROM original_row r JOIN house_doc d ON d.url=r.url AND d.sha=r.sha JOIN original o USING(url,sha)
 WHERE r.tier='verified' AND d.state='parsed';
"""
EXCLUDED = re.compile(r"\b(?:etfs?|etn|index|mutual|funds?|bonds?|treasur(?:y|ies)|notes?|debenture|"
                      r"warrants?|futures?|crypto(?:currency)?|bitcoin|unit investment trust|"
                      r"annuit(?:y|ies)|spdr|ishares|proshares|wisdomtree|stock appreciation right|"
                      r"restricted stock units?|rsus?|closed.end|business development company|bdcs?|"
                      r"rand capital corp(?:oration)?|kkr real estate select trust|mai managed|cref stock|"
                      r"fd class|co-investors llc)\b", re.I)


def classify(e: Entry) -> tuple[str, float | None]:
    amount = re.fullmatch(r"\$([\d,]+(?:\.\d{2})?)(?:\s*[-–]\s*\$[\d,]+|\s*(?:\+|and over))?", e.amount, re.I)
    low = float(amount[1].replace(",", "")) if amount else None
    asset = e.asset
    action = e.action.split(" ", 1)[0]
    excluded_description = re.search(r"\b(?:mistakenly|in error|gifted|donated|not a transaction|"
                                     r"(?:family|charitable) foundation|charitable (?:trust|account))\b",
                                     e.description, re.I) or (action == "P" and re.search(
                                         r"\b(?:inherited|inheritance|gift|vesting|RSU distribution|did not purchase)\b",
                                         e.description, re.I))
    if (e.kind != "trade" or action not in {"P", "S", "E"} or (low is not None and low <= 1000)
            or EXCLUDED.search(asset) or re.search(r"\([A-Z]{4}X\)", asset) or excluded_description):
        return "outside", low
    tags = set(re.findall(r"\[([A-Z0-9]{2})\]", asset))
    if tags - {"ST", "PS", "OP"}:
        return "outside", low
    equity = bool(tags in ({"ST"}, {"PS"}) or re.search(r"\b(common|preferred|ordinary|private) (stock|shares)\b",
                                                         asset, re.I))
    if tags == {"OP"}:
        equity = bool(re.search(r"\bstock option\b", asset, re.I) or (
            re.search(r"\b(inc|corp|company|plc)\b", asset, re.I) and re.search(r"\([A-Z][A-Z0-9./-]{0,8}\)", asset)))
    if low is None or not equity or e.owner not in {"SP", "DC", "JT", "SELF", "unmarked"}:
        return "review", low
    return "qualifies", low


def queue(db, url, row, seen, reason):
    old = db.execute("SELECT metadata FROM house_doc WHERE url=?", (url,)).fetchone()
    db.execute("""INSERT INTO house_doc VALUES(?,?,?,'pending',?,NULL) ON CONFLICT(url) DO UPDATE SET
                metadata=excluded.metadata,state='pending',reason=excluded.reason""", (url, encode(row), seen, reason))
    db.execute("INSERT INTO filing_change(seen,url,kind,old_metadata,new_metadata) VALUES(?,?,?,?,?)",
               (seen, url, reason, old[0] if old else None, encode(row)))


def discover(db, legacy: Path, records: dict, stamp: str) -> None:
    initial = get(db, "house_checked") is None
    with db:
        if initial:
            with closing(sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)) as old:
                db.executemany("INSERT OR IGNORE INTO house_doc VALUES(?,?,?,'baseline','baseline',NULL)",
                               ((u, encode(json.loads(m)), s) for u, s, m in old.execute(
                                   "SELECT url,first_seen,metadata FROM filings")))
        for url, meta in records.items():
            prev = db.execute("SELECT metadata FROM house_doc WHERE url=?", (url,)).fetchone()
            if not prev or prev[0] != encode(meta):
                queue(db, url, meta, stamp, "new_filing" if not prev else "index_correction")
            elif initial and meta["form"] == "PTR" and get(db, "feed_checked") and not db.execute(
                    "SELECT 1 FROM feed WHERE tbl='political_filings' AND id=?", (url,)).fetchone():
                queue(db, url, meta, stamp, "catchup")
        cursor = int(get(db, "house_feed_cursor") or 0)
        for seq, kind, old_json, new_json in db.execute("""SELECT seq,kind,before_json,after_json FROM feed_change
                WHERE tbl='political_filings' AND seq>? ORDER BY seq""", (cursor,)):
            cursor = seq
            if not new_json:
                continue  # a vendor withdrawal alone does not revoke an official filing
            f = json.loads(new_json)
            url = f["sourceUrl"]
            if f["chamber"] != "house" or not re.fullmatch(FORM_URL, url):
                continue
            prev = db.execute("SELECT state,sha,metadata FROM house_doc WHERE url=?", (url,)).fetchone()
            if prev and (prev[0] == "pending" or prev[1] == f["rawSha256"]):
                continue
            if old_json and json.loads(old_json)["rawSha256"] == f["rawSha256"]:
                continue
            row = json.loads(prev[2]) if prev else {"filer": f"{f['filerFirst']} {f['filerLast']}",
                    "district": f["stateDistrict"], "form": "PTR", "filed_on": f["filingDate"]}
            queue(db, url, row, stamp, "catchup" if kind == "added" and prev else
                  "source_correction" if kind == "updated" else "new_filing")
        put(db, "house_feed_cursor", str(cursor))
        put(db, "house_checked", stamp)


def identity(db, url, meta):
    person = db.execute("""SELECT m.bio FROM feed f JOIN member m ON m.bio=json_extract(f.body,'$.memberId')
                       WHERE f.tbl='political_filings' AND f.id=? AND m.chamber='house'""", (url,)).fetchone()
    candidates = {bio for bio, name, short in db.execute(
        "SELECT bio,name,short_name FROM member WHERE chamber='house' AND seat=?", (meta["district"],))
        if normalized(meta["filer"]) in {normalized(name), normalized(short)}}
    if person:
        if candidates and person[0] not in candidates:
            return None
        # A secondary identity must also match the seat and name in the Clerk directory.
    return next(iter(candidates)) if len(candidates) == 1 else None


def record_pdf(db, cache: Path, url: str, meta: dict, reason: str, data: bytes) -> dict:
    match = re.fullmatch(FORM_URL, url)
    if not match or not data.startswith(b"%PDF-"):
        raise ValueError("Unexpected House URL or non-PDF response")
    sha = hashlib.sha256(data).hexdigest()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = cache / (sha + ".pdf")
    if not target.exists():
        tmp = target.with_suffix(".partial")
        tmp.write_bytes(data)
        tmp.replace(target)
    form = meta["form"].split(" (")[0]
    parsed = house_report(data, form, match[3])
    bio = identity(db, url, meta)
    prior_sha = db.execute("SELECT sha FROM house_doc WHERE url=?", (url,)).fetchone()[0]
    existed = db.execute("SELECT 1 FROM original WHERE url=?", (url,)).fetchone()
    correction = bool(existed and prior_sha != sha) or reason in {"index_correction", "source_correction"}
    stamp = now()
    counts = {}
    with db:
        db.execute("INSERT OR IGNORE INTO original VALUES(?,?,?,?,?,?)",
                   (url, sha, stamp, bio, reason, encode(parsed.issues)))
        for ordinal, e in enumerate(parsed.entries):
            if e.kind != "trade":
                continue
            tier, low = classify(e)
            if parsed.issues or not bio:
                tier = "review" if tier != "outside" else tier
            elif tier == "qualifies":
                tier = "verified"
                if correction or e.status in {"amended", "deleted"} or form == "Amendment":
                    tier = "correction_review"
                elif form != "PTR" or db.execute("""SELECT 1 FROM original_row WHERE bio=?
                        AND trade_date=? AND asset=? AND url<>? LIMIT 1""",
                                                  (bio, e.date, e.asset, url)).fetchone():
                    tier = "overlap_review"
            db.execute("INSERT OR REPLACE INTO original_row VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (url, sha, ordinal, bio, tier, e.date, e.asset, low, e.owner, e.page, encode(asdict(e))))
            counts[tier] = counts.get(tier, 0) + 1
        db.execute("UPDATE house_doc SET sha=?,state=? WHERE url=?", (sha, "review" if parsed.issues else "parsed", url))
    return {"url": url, "observation": reason, "rows": counts, "issues": parsed.issues}


def process_pending(db, state: Path) -> tuple[list, list, list]:
    done, deferred, errors = [], [], []
    for url, meta, reason, first in db.execute(
            "SELECT url,metadata,reason,first_seen FROM house_doc WHERE state='pending' ORDER BY url").fetchall():
        try:
            if not re.fullmatch(FORM_URL, url):
                raise ValueError("Unexpected official URL")
            done.append(record_pdf(db, state / "original-pdfs", url, json.loads(meta), reason, download(url)))
        except (OSError, ValueError, sqlite3.Error) as exc:
            cause = exc.__cause__
            age = datetime.now(timezone.utc) - datetime.fromisoformat(first)
            if (isinstance(cause, HTTPError) and cause.code == 404 and
                    timedelta(0) <= age < timedelta(hours=72)):
                deferred.append({"url": url, "status": "indexed; PDF not yet available", "first_seen": first})
            else:
                errors.append({"url": url, "error": str(exc)})
    return done, deferred, errors
