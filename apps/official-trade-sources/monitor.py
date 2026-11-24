"""Check published congressional filing notices, not brokerage transactions."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import date, datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen

from senate import read_senate_table

HOUSE = "https://disclosures-clerk.house.gov/public_disc"
MAX_BYTES = 16 * 1024 * 1024
Snapshot = dict[str, dict[str, str]]


def download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "official-trade-sources/0.1 (filing research)"})
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except (OSError, HTTPException) as exc:
        raise ValueError(f"Could not read {url}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise ValueError(f"Download too large: {url}")
    return data


def house_index(year: int, data: bytes, last_name: str, *, include_annual: bool = False) -> Snapshot:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        filename = f"{year}FD.xml"
        if filename not in archive.namelist() or archive.getinfo(filename).file_size > MAX_BYTES:
            raise ValueError(f"Missing or oversized {filename} in House archive")
        root = ET.fromstring(archive.read(filename))
    if root.tag != "FinancialDisclosure" or root.find("Member") is None:
        raise ValueError(f"Unrecognized or empty {year} House index")

    records: Snapshot = {}
    for member in root:
        filing_type = member.findtext("FilingType")
        if member.tag != "Member" or not filing_type:
            raise ValueError(f"Unrecognized row in {year} House index")
        if filing_type not in {"P", "A"} and not (include_annual and filing_type == "O"):
            continue
        first, last, doc_id, filed = (
            (member.findtext(field) or "").strip() for field in ("First", "Last", "DocID", "FilingDate")
        )
        if not (first and last and filed and re.fullmatch(r"[0-9]+", doc_id)):
            raise ValueError(f"Incomplete filing in {year} House index")
        if last_name and last.casefold() != last_name.casefold():
            continue
        folder = "ptr-pdfs" if filing_type == "P" else "financial-pdfs"
        url = f"{HOUSE}/{folder}/{year}/{doc_id}.pdf"
        record = {
            "form": {"P": "PTR", "O": "Annual", "A": "Amendment (not necessarily a PTR)"}[filing_type],
            "filer": f"{first} {last}",
            "district": (member.findtext("StateDst") or "").strip(),
            "filed_on": datetime.strptime(filed, "%m/%d/%Y").date().isoformat(),
        }
        if url in records and records[url] != record:
            raise ValueError(f"Conflicting House index rows for {url}")
        records[url] = record
    return records


def house_snapshot(years: list[int], last_name: str, check_pdfs: bool, include_annual: bool = False) -> Snapshot:
    if check_pdfs and not last_name:
        raise ValueError("--check-pdfs requires --last-name to avoid bulk PDF requests")
    records: Snapshot = {}
    for year in years:
        if not 2012 <= year <= date.today().year:
            raise ValueError(f"Year must be between 2012 and the current year: {year}")
        records.update(house_index(year, download(f"{HOUSE}/financial-pdfs/{year}FD.zip"), last_name,
                                   include_annual=include_annual))
    if check_pdfs:
        for url, record in records.items():
            data = download(url)
            if not data.startswith(b"%PDF-"):
                raise ValueError(f"Expected a PDF at {url}")
            record["pdf_sha256"] = hashlib.sha256(data).hexdigest()
    return records


def observe(path: Path, watch: dict[str, object], records: Snapshot) -> dict[str, object]:
    """Commit one successful observation. Missing rows are not evidence of withdrawal."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config = json.dumps(watch, sort_keys=True)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    events = []
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS watch (config TEXT NOT NULL, last_checked TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS filings (url TEXT PRIMARY KEY, first_seen TEXT, metadata TEXT)")
        saved = db.execute("SELECT config FROM watch").fetchone()
        baseline = saved is None
        if baseline:
            db.execute("INSERT INTO watch VALUES (?, ?)", (config, now))
        elif saved[0] != config:
            raise ValueError("State belongs to a different watch; use a separate --state file")
        else:
            db.execute("UPDATE watch SET last_checked = ?", (now,))
        for url, record in sorted(records.items()):
            metadata = json.dumps(record, sort_keys=True)
            old = db.execute("SELECT first_seen, metadata FROM filings WHERE url = ?", (url,)).fetchone()
            if old is None:
                db.execute("INSERT INTO filings VALUES (?, ?, ?)", (url, now, metadata))
                if not baseline:
                    events.append({"kind": "newly_seen", "url": url, "first_seen": now, "filing": record})
            elif old[1] != metadata:
                db.execute("UPDATE filings SET metadata = ? WHERE url = ?", (metadata, url))
                events.append({"kind": "updated", "url": url, "first_seen": old[0],
                               "before": json.loads(old[1]), "filing": record})
    return {
        "status": "baseline" if baseline else "changed" if events else "unchanged",
        "source": watch["source"], "checked_at": now, "filings_checked": len(records), "events": events,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="local SQLite file, one per watch")
    sources = parser.add_subparsers(dest="source", required=True)
    house = sources.add_parser("house", help="check the House Clerk's live PTR index")
    house.add_argument("--year", type=int, action="append", help="repeatable; default: current and previous year")
    house.add_argument("--last-name", default="", help="exact last name (case insensitive); default: all")
    house.add_argument("--check-pdfs", action="store_true", help="also detect byte changes in matching PDFs")
    house.add_argument("--include-annual", action="store_true", help="watch annual disclosures as well as PTRs/amendments")
    senate = sources.add_parser("senate", help="compare locally saved Senate eFD results (no network)")
    senate.add_argument("tables", nargs="+", type=Path, help="rendered results table HTML, one file per page")
    args = parser.parse_args(argv)
    watch: dict[str, object] = {"source": args.source}
    try:
        if args.source == "house":
            year = date.today().year
            years = sorted(set(args.year)) if args.year else [year - 1, year]
            last = args.last_name.strip().casefold()
            watch.update(years=years if args.year else "rolling", last=last, pdfs=args.check_pdfs)
            if args.include_annual:
                watch["annual"] = True  # preserve compatibility with old PTR-only watch states
            records = house_snapshot(years, last, args.check_pdfs, args.include_annual)
        else:
            records = {}
            for path in args.tables:
                for url, record in read_senate_table(path.read_text(encoding="utf-8-sig")).items():
                    if url in records and records[url] != record:
                        raise ValueError(f"Conflicting Senate rows for {url}; recapture the pages together")
                    records[url] = record
        result = observe(args.state, watch, records)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, sqlite3.Error) as exc:
        print(f"Check failed; no observation saved: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
