# Congressional disclosure monitor

One local, noninteractive run checks both chambers without submitting the Senate eFD
agreement, using a paid API, sending notifications, or placing trades. House Clerk index
entries are not transactions. The runner compares the current and previous filing years
against the migrated index, independently refreshes the public two-chamber
[archive](https://huggingface.co/datasets/austin-starks/congressional-stock-trades),
and checks qualifying new House rows against their original PDF. Senate archive rows
remain unverified leads. Weekly membership and Capitol Signals checks retry after a
missed Monday. Reporting delays mean even a daily check is not a live trade feed.

## Private state

Supply the existing `congress-house-filings.sqlite` and `snapshot-YYYY-MM-DD/`
(the pinned `snapshot.json` and three tables under `data/`) outside Git. Transfer the
historical member/watchlist databases and source PDFs privately; they remain unchanged.
A fresh live database silently imports the pinned archive before diffing the current
release. Retain ALL files in the state directory on upgrades. Only the source code and
synthetic tests belong in the public repository.

```sh
uv run --with-requirements requirements-watchlist.txt python daily.py --state "$PRIVATE_STATE"
sqlite3 -readonly "$PRIVATE_STATE/daily.sqlite" \
  'SELECT filer,trade_date,filed_on,asset,owner,amount_low,url,page,observation FROM verified_trade;'
sqlite3 -readonly "$PRIVATE_STATE/daily.sqlite" \
  'SELECT started,ok,json_extract(report,"$.errors") FROM runs ORDER BY id DESC LIMIT 5;'
```

`feed` holds the complete latest public archive; `feed_change` retains earlier row versions,
withdrawals and first-observed timestamps for subsequent releases. `original_row` retains
PDF page, hash, owner, action, amount, notification date, description and review tier;
`filing_change` and `runs` distinguish intake, catch-up and corrections. Exact original-backed
entries on an unambiguous current House member are eligible. Annual repeats, amendment rows,
ambiguous identities and unreadable original layouts require review. File revisions with identical
index metadata may not be noticed until the archive updates its hash. A failure in one source
does not mean no trades; the run exits nonzero and retains the error. When the Clerk
publishes an index entry before its PDF (HTTP 404), coverage is explicitly **partial**.
The job retains the filing in its pending queue, retries every run, and escalates it
to a source error after 72 hours. No transaction is inferred from an unavailable PDF.
Use for permitted purposes under [5 USC 13107(c)](https://www.law.cornell.edu/uscode/text/5/13107#c).

## VPS

Linux/systemd, Python 3.12 and `uv` are required. Check out or extract a reviewed immutable
revision under a service-readable, root-owned directory. Seed `/var/lib/official-trade-sources`
privately (mode `0700`); for live SQLite databases use the SQLite backup API, not a copy
of a database without its WAL. Install and run a check before enabling the calendar:

```sh
sh deploy/install
systemctl start official-trade-sources.service
systemctl show official-trade-sources.service -p Result -p ExecMainStatus
systemctl enable --now official-trade-sources.timer
systemctl list-timers --all official-trade-sources.timer
```

The persistent timer runs every day at **10:30 America/New_York**, including weekends
and holidays. It catches up after downtime. No web server is exposed. To read the data
from another machine, transfer a SQLite backup over your existing authenticated SSH
connection. Stop the previous scheduler only after the first check succeeds and the next
timer is visible. Rollback: `systemctl disable --now official-trade-sources.timer`;
leave state intact and point `current` back to the previous immutable release if needed.
