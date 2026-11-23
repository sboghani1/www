# Sports data sources (NFL lines, results, starters)

Verified: 2026-11-23.

Where NFL schedules, scores, betting lines and line history come from, and
what surprises when using them. Book names matter: two books can disagree
about the same game, so any line-movement claim must name its book.

## ESPN public API (no key)

- **Week schedule and scores:**
  `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=<season>&seasontype=2&week=<n>`.
  Each event has `competitions[0].competitors[]` with `homeAway` and `score`.
  Before kickoff its `odds[0].spread` sign is not reliable; read the side
  from `odds[0].details` (for example `DET -2.5`). Verified by comparing
  several games' `spread` with `details` on the same response.
- **Lines per game:** `.../summary?event=<id>` → `pickcenter[0]`, provider
  DraftKings.
  - `spread` (home-relative) and `overUnder` are the **closing** line once
    the game has started. Checked on 137 finished games: `spread` equalled
    `pointSpread.home.close.line` every time.
  - `pointSpread.{home,away}` and `total.{over,under}` carry `open`, `close`
    and, while the game is live, `live` lines with prices. Before kickoff
    `close` holds the current line.
- **Starting quarterback:** in the same summary,
  `boxscore.players[].statistics[]` with `name == "passing"`; the athlete
  with the most attempts (from `completions/passingAttempts`) is the
  starter. Useful for finding QB changes week to week.

## Forwarder line snapshots (current season)

- The forwarder's NFL lines fetcher appends every poll to the
  `nfl_line_snapshots` tab of its intake Sheet: `captured_at`, event, teams,
  `bookmaker`, and packed `spread,price,moneyline` groups per side (game,
  1H, Q1) plus `total,over,under`; `nodata` marks missing values.
- Polling cadence (`poll_interval_for_game` in `nfl_lines.py`): daily until
  a week out, every 12 hours inside the week, hourly inside the last day,
  every 30 minutes inside the last 4 hours, then a final poll.
- The book changed from BetOnline to Bovada on 2026-10-27, so movement
  across that date mixes books.
- Read it as the `forwarder` account with that account's own `.env` (its
  `get_gspread_client`). The workspace service account in the agent
  environment gets HTTP 403 on that Sheet (observed 2026-11-14).
- `telegram-channel-forwarder/data/nfl_lines_history.csv` and
  `nfl_open_close.json` cover past seasons only, not the current one.

## Book differences

The same pick can look "moved toward" at one book and flat at another (for
example DraftKings unchanged while Bovada moved half a point). Results that
hold against one book's close and not another's are probably noise; see
`capper-backtesting.md`.

*Connections:* youtube-pick-extraction.md, capper-backtesting.md,
vps-operations.md (the `forwarder` account).
