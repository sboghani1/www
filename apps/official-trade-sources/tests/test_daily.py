import hashlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pyarrow as pa
import pyarrow.parquet as pq

import archive_live as archive
import house_live as house
import weekly
from report_rows import Entry, Parsed

URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/1234.pdf"
META = {"form": "PTR", "filer": "Example Member", "district": "XX01", "filed_on": "2026-01-06"}


def manifest(amount):
    doc = {"schemaVersion": 2, "audit": {"passed": True}, "lastSourceCheckAt": "2026-01-06T00:00:00Z",
           "tables": {}, "freshness": {"byChamber": {}}}
    files = {}
    for t in archive.TABLES:
        row = {"sourceUrl": URL, "docId": "1234", "chamber": "house", "rowIndex": 0,
               "eventId": "synthetic", "version": 1, "amountLow": amount}
        output = io.BytesIO()
        pq.write_table(pa.Table.from_pylist([row]), output)
        name = f"data/{t}/2026-00000-of-00001.parquet"
        blob = files[name] = output.getvalue()
        doc["tables"][t] = {"rows": 1, "manifests": [{"year": 2026, "files": [
            {"publicPath": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}]}]}
    return doc, lambda f: files[f["publicPath"]]


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(archive.SCHEMA + house.SCHEMA + weekly.SCHEMA)
        self.db.execute("INSERT INTO member VALUES('E000001','house','XX01','Example Member','Example Member')")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_silent_baseline_repeat_and_atomic_failed_correction(self):
        doc, reader = manifest(1001)
        archive.apply_release(self.db, doc, "baseline", reader)
        archive.apply_release(self.db, doc, "repeat", lambda _: self.fail("unchanged shard downloaded"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM feed_change").fetchone()[0], 0)
        doc2, next_reader = manifest(2001)
        with self.assertRaisesRegex(ValueError, "Checksum"):
            archive.apply_release(self.db, doc2, "corrupt", lambda f: (
                b"bad" if "trade_events" in f["publicPath"] else next_reader(f)))
        self.assertEqual(archive.get(self.db, "feed_release"), "repeat")
        self.assertEqual(self.db.execute("SELECT distinct json_extract(body,'$.amountLow') FROM feed").fetchall(), [(1001,)])
        archive.apply_release(self.db, doc2, "fixed", next_reader)
        self.assertEqual(self.db.execute("SELECT kind,json_extract(before_json,'$.amountLow'),"
                                         "json_extract(after_json,'$.amountLow') FROM feed_change").fetchall(),
                         [("updated", 1001, 2001)] * 3)

    def test_already_indexed_catchup_and_household_lots(self):
        legacy = self.root / "legacy.sqlite"
        old = sqlite3.connect(legacy)
        old.execute("CREATE TABLE filings(url TEXT,first_seen TEXT,metadata TEXT)")
        old.execute("INSERT INTO filings VALUES(?,?,?)", (URL, archive.now(), archive.encode(META)))
        old.commit()
        old.close()
        archive.put(self.db, "feed_checked", archive.now())
        house.discover(self.db, legacy, {URL: META}, archive.now())
        e = Entry("trade", "Example common stock [ST]", "SP", "$1,001 - $15,000", "2026-01-02", 2, "P")
        with patch.object(house, "house_report", return_value=Parsed(entries=[e, e])):
            house.record_pdf(self.db, self.root, URL, META, "catchup", b"%PDF-synthetic")
        house.discover(self.db, legacy, {URL: META}, archive.now())
        self.assertEqual(self.db.execute("SELECT observation,owner,page FROM verified_trade").fetchall(),
                         [("catchup", "SP", 2)] * 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM filing_change").fetchone()[0], 1)
        e.status = "deleted"
        with patch.object(house, "house_report", return_value=Parsed(entries=[e])):
            house.record_pdf(self.db, self.root, URL, META, "source_correction", b"%PDF-revised")
        self.assertEqual(self.db.execute("SELECT count(*) FROM verified_trade").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM original").fetchone()[0], 2)

    def test_new_index_before_pdf_is_pending_not_verified_and_escalates(self):
        house.queue(self.db, URL, META, archive.now(), "new_filing")
        def not_yet(url):
            raise ValueError("PDF not published") from HTTPError(url, 404, "Not Found", {}, None)
        with patch.object(house, "download", side_effect=not_yet):
            result, deferred, errors = house.process_pending(self.db, self.root)
            self.assertEqual((result, errors), ([], []))
            self.assertEqual([i["url"] for i in deferred], [URL])
            self.assertEqual(self.db.execute("SELECT state,sha FROM house_doc").fetchone(), ("pending", None))
            self.db.execute("UPDATE house_doc SET first_seen=?",
                            ((datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),))
            _, _, errors = house.process_pending(self.db, self.root)
            self.assertEqual([i["url"] for i in errors], [URL])

    def test_ambiguous_and_nontrade_never_count(self):
        for asset, amount, action, expected in [
            ("Example ETF [ST]", "$1,001 - $15,000", "P", "outside"),
            ("Example common stock [ST]", "$1,000.00", "P", "outside"),
            ("Unidentified option [OP]", "$1,001 - $15,000", "P", "review"),
            ("Example common stock [ST]", "$2,119.32", "S (partial)", "qualifies"),
        ]:
            self.assertEqual(house.classify(Entry("trade", asset, "DC", amount, "2026-01-02", 1, action))[0], expected)
