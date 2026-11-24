"""Atomically reconcile checksummed releases. Archive transcriptions are never primary evidence."""

import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pyarrow.parquet as pq

REPO = "austin-starks/congressional-stock-trades"
TABLES = ("political_filings", "political_trades", "political_trade_events")
SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS part (tbl TEXT, year INT, digest TEXT, rows INT, PRIMARY KEY(tbl,year));
CREATE TABLE IF NOT EXISTS feed (tbl TEXT, id TEXT, year INT, url TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(tbl,id));
CREATE INDEX IF NOT EXISTS feed_url ON feed(tbl,url);
CREATE TABLE IF NOT EXISTS feed_change (seq INTEGER PRIMARY KEY, release TEXT, seen TEXT, tbl TEXT,
 id TEXT, kind TEXT, before_json TEXT, after_json TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=iso)


def iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported field type {type(value).__name__}")


def get(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM checkpoint WHERE name=?", (key,)).fetchone()
    return row[0] if row else None


def put(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO checkpoint VALUES(?,?)", (key, value))


def fetch(url: str, limit: int = 16 << 20) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "official-trade-sources/1.0"}), timeout=45) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"Source exceeds size limit: {url}")
    return body


def identifier(table: str, row: dict) -> str:
    if not isinstance(row["sourceUrl"], str) or not row["sourceUrl"].startswith("https://"):
        raise ValueError("Missing official filing link")
    if table == TABLES[0]:
        return row["sourceUrl"]
    if table == TABLES[1]:
        return f"{row['chamber']}:{row['docId']}:{int(row['rowIndex'])}"
    return f"{row['eventId']}@{int(row['version'])}"


def apply_release(db: sqlite3.Connection, doc: dict, release: str, read_file) -> dict:
    if doc["schemaVersion"] != 2 or doc["audit"]["passed"] is not True:
        raise ValueError("Unsupported or unaudited archive release")
    checked = datetime.fromisoformat(doc["lastSourceCheckAt"].replace("Z", "+00:00"))
    prior = get(db, "feed_checked")
    if prior and checked < datetime.fromisoformat(prior):
        raise ValueError("Archive check time went backwards")
    old_parts = {(t, y): (digest, count) for t, y, digest, count in db.execute("SELECT * FROM part")}
    baseline = get(db, "feed_release") is None
    current_parts = {}
    stamp = now()
    with db:
        db.execute("DROP TABLE IF EXISTS temp.incoming")
        db.execute("CREATE TEMP TABLE incoming(tbl TEXT,id TEXT,year INT,url TEXT,body TEXT, PRIMARY KEY(tbl,id))")
        db.execute("DROP TABLE IF EXISTS temp.touched")
        db.execute("CREATE TEMP TABLE touched(tbl TEXT,year INT,PRIMARY KEY(tbl,year))")
        for tbl in TABLES:
            table = doc["tables"][tbl]
            total = 0
            for group in table["manifests"]:
                year = int(group["year"])
                signature = hashlib.sha256(encode([(f["publicPath"], f["size"], f["sha256"]) for f in
                                                   group["files"]]).encode()).hexdigest()
                part = (tbl, year)
                if part in current_parts or not group["files"]:
                    raise ValueError("Duplicate or empty archive group")
                count = 0
                if old_parts.get(part, (None,))[0] == signature:
                    count = old_parts[part][1]
                else:
                    db.execute("INSERT INTO touched VALUES(?,?)", part)
                    for f in group["files"]:
                        name = f["publicPath"]
                        if (not re.fullmatch(rf"data/{tbl}/{year}-[\w-]+\.parquet", name)
                                or not re.fullmatch("[a-f0-9]{64}", f["sha256"])
                                or not 0 < f["size"] <= 128 << 20):
                            raise ValueError("Invalid release file descriptor")
                        data = read_file(f)
                        if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
                            raise ValueError(f"Checksum mismatch: {name}")
                        for batch in pq.ParquetFile(io.BytesIO(data)).iter_batches(batch_size=2048):
                            db.executemany("INSERT INTO incoming VALUES(?,?,?,?,?)", (
                                (tbl, identifier(tbl, row), year, row["sourceUrl"], encode(row))
                                for row in batch.to_pylist()))
                            count += batch.num_rows
                current_parts[part] = (signature, count)
                total += count
            if not table["manifests"] or total != table["rows"]:
                raise ValueError(f"Row count mismatch: {tbl}")
        if set(old_parts) - set(current_parts):
            raise ValueError("Archive lost a historical year; investigate before advancing")
        if not baseline:
            db.execute("""INSERT INTO feed_change(release,seen,tbl,id,kind,before_json,after_json)
                SELECT ?,?,i.tbl,i.id,CASE WHEN f.id IS NULL THEN 'added' ELSE 'updated' END,f.body,i.body
                FROM incoming i LEFT JOIN feed f USING(tbl,id) WHERE f.id IS NULL OR i.body<>f.body""",
                       (release, stamp))
            db.execute("""INSERT INTO feed_change(release,seen,tbl,id,kind,before_json,after_json)
                SELECT ?,?,f.tbl,f.id,'withdrawn',f.body,NULL FROM feed f JOIN touched USING(tbl,year)
                WHERE NOT EXISTS (SELECT 1 FROM incoming i WHERE i.tbl=f.tbl AND i.id=f.id)""", (release, stamp))
        db.execute("DELETE FROM feed WHERE (tbl,year) IN (SELECT tbl,year FROM touched)")
        db.execute("INSERT INTO feed SELECT * FROM incoming")
        db.executemany("INSERT OR REPLACE INTO part VALUES(?,?,?,?)",
                       [(t, y, h, n) for (t, y), (h, n) in current_parts.items()])
        put(db, "feed_release", release)
        put(db, "feed_checked", checked.isoformat())
        put(db, "feed_freshness", encode(doc["freshness"]))
        changed_parts = db.execute("SELECT count(*) FROM touched").fetchone()[0]
    return {"release": release, "baseline": baseline, "changed_year_tables": changed_parts,
            "cutoffs": {k: v["filingDate"] for k, v in doc["freshness"]["byChamber"].items()},
            "upstream_failed_filings": doc.get("totals", {}).get("failedFilings", 0)}


def update(db: sqlite3.Connection, seed: Path) -> dict:
    db.executescript(SCHEMA)
    if get(db, "feed_release") is None:
        doc = json.loads((seed / "snapshot.json").read_bytes())
        apply_release(db, doc, "migration:" + doc["sourceFingerprint"], lambda f: (seed / f["publicPath"]).read_bytes())
    revision = json.loads(fetch(f"https://huggingface.co/api/datasets/{REPO}"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Unrecognized dataset revision")
    root = f"https://huggingface.co/datasets/{REPO}/resolve/{revision}"
    doc = json.loads(fetch(root + "/snapshot.json"))
    checked = datetime.fromisoformat(doc["lastSourceCheckAt"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - checked
    if not -timedelta(hours=2) <= age <= timedelta(hours=72):
        raise ValueError(f"Archive stale or future-dated: {checked.isoformat()}")
    return apply_release(db, doc, revision, lambda f: fetch(f"{root}/{f['publicPath']}", f["size"]))
