# Sports data sources (NFL and NBA lines, results, starters)

Verified: 2026-11-23.

Where NFL and NBA schedules, scores, betting lines and line history come
from, and what surprises when using them. Book names matter: two books can disagree
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

## ESPN NBA

- Same API shape under `.../sports/basketball/nba/`: `scoreboard?dates=YYYYMMDD`
  and `summary?event=<id>`.
- Before tip-off the scoreboard's `odds[0].spread` is home-relative
  (matched `odds[0].details` on all ten games checked on 2026-11-23, unlike
  the NFL scoreboard). Once a game starts the scoreboard drops `odds`; read
  the close from the summary's `pickcenter[0]` (provider "Draft Kings",
  `spread` home-relative, `overUnder`).
- The summary's `injuries[]` lists each player's status with a timestamp,
  which usually explains a large same-day line move.
- Box-score athletes carry `didNotPlay`, which settles picks conditional on
  a player playing.

## Bovada public feed

Current pregame lines without a key:
`https://www.bovada.lv/services/sports/event/v2/events/A/description/<sport>/<league>?marketFilterId=def&preMatchOnly=true&lang=en`
(for example `basketball/nba`, `football/nfl`). Use
`displayGroups[].markets[]` with `period.description == "Game"`; outcomes
carry `price.handicap` and `price.american`. Request with a browser
user-agent and compression. Bovada and DraftKings often differ by a point or
more on the same game (one had a home team -1 while the other had it +1.5).

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
  `get_gspread_client`), or from the agent account with the workspace
  `GOOGLE_CREDENTIALS` (base64 JSON) and `NFL_INTAKE_SHEET_ID` in the
  `google-sheet-check` venv: on 2026-11-23 that read `nfl_line_snapshots`,
  `nba_line_snapshots` and `nba_games`. An HTTP 403 was seen on 2026-11-14,
  cause unknown; fall back to the `forwarder` account if it recurs.
- `telegram-channel-forwarder/data/nfl_lines_history.csv` and
  `nfl_open_close.json` cover past seasons only, not the current one.

## Forwarder NBA tabs

- `nba_games`: one row per game with packed `latest_*` and `opening_*`
  cells (`spread,price,moneyline|h1…|q1…` per side, `total,over,under`).
  `nba_line_snapshots` gets a row only when a game's line changes, so the
  last row before a time is the line at that time.
- The NBA fetcher runs every 5 minutes (`nba-lines-fetcher.timer`,
  `*:03/5` UTC) from Bovada since 2026-10-27 (`NBA_LINES_SOURCE`).
- A game Bovada never posted has **no row**. A game Bovada posted then pulled
  keeps its row with `nodata` cells and drops out of the fetcher's game list
  until re-posted. Pulls lined up within minutes of ESPN injury updates and
  came back with moves of up to 4 points, so a pulled line means news.
- Bovada posted next-day NBA lines around 8–10:30 PM Pacific; BetOnline's
  appeared between 3 PM and midnight, sometimes before Bovada's (observed
  2026-11-12 to 11-18).

## The Odds API (BetOnline, Hard Rock)

- Paid; the forwarder's line fetchers no longer use it. A working key is in
  the forwarder's guesser env; another env file there holds a dead key (HTTP
  401). Never print keys.
- `GET /v4/sports` is free and its response headers report
  `x-requests-remaining`.
- `/v4/sports/basketball_nba/odds?bookmakers=betonlineag,bovada,hardrockbet&markets=spreads`
  costs 1 credit per market for up to 10 books in one call; a call that
  returns no events costs 0. Use `commenceTimeFrom/To` to limit the window.
- Its `commence_time` for NBA games is about 10 minutes after ESPN's tip
  time, so match games by teams and a time window, not exact time.

## Book differences

The same pick can look "moved toward" at one book and flat at another (for
example DraftKings unchanged while Bovada moved half a point). Results that
hold against one book's close and not another's are probably noise; see
`capper-backtesting.md`.

*Connections:* youtube-pick-extraction.md, capper-backtesting.md,
vps-operations.md (the `forwarder` account), nba-picker-tracking.md,
bell-bait-tracker.md (uses the NBA tabs and the Odds API).
