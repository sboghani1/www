"""Run once and persist a local audit. No service listener, credentials or external notifications."""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import archive_live as archive
import house_live as house
import weekly
from monitor import house_snapshot
from report_rows import Entry

SCHEMA = """CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,
 ok INT,report TEXT);"""
SOURCE_ERRORS = (OSError, ValueError, KeyError, sqlite3.Error, ET.ParseError, zipfile.BadZipFile)


def lead_changes(db, after: int):
    leads, corrections = [], []
    records = db.execute("""SELECT kind,before_json,after_json FROM feed_change
                         WHERE seq>? AND tbl='political_trade_events' ORDER BY seq""", (after,))
    for kind, old, new in records:
        e = json.loads(new or old)
        if not db.execute("SELECT 1 FROM member WHERE bio=?", (e["memberId"],)).fetchone():
            continue
        brief = {"member": e["memberId"], "transaction_date": e["transactionDate"], "url": e["sourceUrl"],
                 "event": e["eventId"], "version": e["version"], "change": kind}
        if kind != "added" or e["version"] > 1 or e["supersededAt"]:
            corrections.append(brief)
            continue
        if e["amountLow"] is None or e["amountLow"] <= 1000 or e["action"] not in {"purchase", "sale", "exchange"}:
            continue
        if db.execute("SELECT 1 FROM verified_trade WHERE url=? AND ordinal=? AND trade_date=?",
                      (e["sourceUrl"], e["sourceRowIndex"], e["transactionDate"])).fetchone():
            continue
        code = e["assetTypeCode"] or {"Stock": "ST", "Stock Option": "OP",
                                      "Non-Public Stock": "PS"}.get(e["assetTypeLabel"])
        entry = Entry("trade", f"{e['assetDescription']} [{code or ''}]", "SELF", f"${e['amountLow']:,.2f}",
                      e["transactionDate"], 0, {"purchase": "P", "sale": "S", "exchange": "E"}[e["action"]])
        if house.classify(entry)[0] == "qualifies":
            leads.append(brief)
    return leads, corrections


def run(db, state: Path, seed: Path) -> dict:
    db.executescript(archive.SCHEMA + house.SCHEMA + weekly.SCHEMA + SCHEMA)
    started = archive.now()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    iso = today.isocalendar()
    week = f"{iso.year}-W{iso.week:02}"
    errors = {}
    report = {"started": started, "errors": errors}
    try:
        report["current_members"] = weekly.rosters(db, week)
    except SOURCE_ERRORS as exc:
        errors["membership"] = str(exc)
    try:
        report["archive"] = archive.update(db, seed)
    except SOURCE_ERRORS as exc:
        errors["archive"] = str(exc)
    try:
        records = house_snapshot([today.year - 1, today.year], "", False, True)
        if not archive.get(db, "feed_checked") and not archive.get(db, "house_checked"):
            raise ValueError("Cannot reconcile the migration without its historical feed baseline")
        house.discover(db, state / "congress-house-filings.sqlite", records, started)
        report["indexed_house_filings"] = len(records)
    except SOURCE_ERRORS as exc:
        errors["house_index"] = str(exc)
    if "membership" not in errors:
        report["originals_processed"], report["awaiting_publication"], failed = house.process_pending(db, state)
        if failed:
            errors["originals"] = failed
    if "archive" not in errors:
        try:
            report["weekly"] = weekly.signals(db, week)
        except SOURCE_ERRORS as exc:
            errors["weekly"] = str(exc)
    fcursor = int(archive.get(db, "reported_filings") or 0)
    report["filings"] = [{"url": url, "kind": kind, **json.loads(meta)} for url, kind, meta in
                         db.execute("SELECT url,kind,new_metadata FROM filing_change WHERE seq>? ORDER BY seq",
                                    (fcursor,))]
    cursor = int(archive.get(db, "reported_feed") or 0)
    report["unverified_leads"], report["archive_corrections"] = lead_changes(db, cursor)
    report["pending_originals"] = db.execute("SELECT count(*) FROM house_doc WHERE state='pending'").fetchone()[0]
    report["ok"] = not errors
    report["coverage"] = "partial" if errors or report["pending_originals"] else "checked"
    with db:
        db.execute("INSERT INTO runs(started,finished,ok,report) VALUES(?,?,?,?)",
                   (started, archive.now(), not errors, archive.encode(report)))
        archive.put(db, "reported_filings", str(db.execute("SELECT coalesce(max(seq),0) FROM filing_change").fetchone()[0]))
        if "membership" not in errors:
            archive.put(db, "reported_feed", str(db.execute("SELECT coalesce(max(seq),0) FROM feed_change").fetchone()[0]))
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--seed", type=Path, help="private pinned snapshot; required only for first import")
    args = p.parse_args()
    os.umask(0o077)
    if args.seed is None:
        seeds = sorted(args.state.glob("snapshot-????-??-??/snapshot.json"))
        if len(seeds) != 1:
            p.error("Specify the exact migrated snapshot with --seed")
        args.seed = seeds[0].parent
    with (args.state / "daily.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(sqlite3.connect(args.state / "daily.sqlite", timeout=30)) as db:
            report = run(db, args.state, args.seed)
    # The actual reports and financial records stay in the access-controlled database.
    print(json.dumps({"ok": report["ok"], "coverage": report["coverage"], "filings": len(report["filings"]),
                      "originals": len(report.get("originals_processed", [])),
                      "pending": report["pending_originals"], "failed_sources": list(report["errors"])}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
