# Bell's bait tracker (NBA)

Verified: 2026-11-23 (code in `apps/decision-helper-bot`, production
`decisions.db`, service logs).

Bell (`@decision_helpers_bot`, called Mel before 2026-11-20) is the private
decision-helper bot. Its `/bait` board lets the user mark NBA sides that look
too easy, re-take or pass as lines move, and learn which kinds of "easy"
picks lose. The user-facing behaviour is in the app README; this topic covers
how it works underneath and how to operate it.

## Where things live

- Code: `apps/decision-helper-bot/decision_helper/` (`bait.py` store and
  formatting, `books.py` Odds API calls, `bot.py` handlers and jobs).
  Deployed release: `/opt/decision-helper-bot/current`.
- State: `/var/lib/decision-helper-bot/` (agent account):
  - `decisions.db`: `baits`, `bait_watch`, `bait_messages`, `book_checks`
    (plus the pick/lean tables).
  - `betonline-fallback.json`: the last scheduled BetOnline read.
  - `bait-morning.json`: the day the 9 AM catch-up last ran.
  - `picker-picks.json`: Trapp & Cowboy export (see
    `youtube-pick-extraction.md`).
- Read the database with `sqlite3.connect("file:…/decisions.db?mode=ro",
  uri=True)`. Back up the file (sqlite `backup()`) before editing rows by
  hand; the service can keep running (WAL).

## Data model

- `baits`: one row per tap. `action` is `take` or `pass` (a pass is graded
  as the side dropped); `ordinal` orders a game's rows and the highest is the
  current position. Each row keeps `selection`, `pick_type` (`spread`, or
  `winner` for a pick'em with no spread), `line`, `price`, `book`
  (`Bovada` or `BetOnline`), `tags_json`, free-text `reason`, a
  `lines_json` snapshot of both sides at the tap, the closing number and the
  grade.
- Tag keys are stored, labels are code (`BAIT_TAGS`). Old ambiguous keys
  stay on their rows as "(old)" labels (`LEGACY_TAGS`); old keys with the
  same meaning are rewritten at start-up (`TAG_ALIASES`).
- `bait_watch`: one row per baited game: `next_check_at`, `alert_line`
  (the number of the last alert; NULL means "last bait row"),
  `picker_signature` (last Trapp & Cowboy state shown).
- `bait_messages`: Bell's message ids a text or voice reply attaches to as a
  reason.
- `book_checks`: one three-book snapshot per NBA game (below).

## When Bell messages unprompted

- **Line checks:** every 3 hours until 4 hours before tip, then 4, 3, 2 and
  1 hours and 15 minutes before tip (`next_check`). An alert needs the
  side's spread a full point from the last number the user saw (last tap or
  last alert); juice-only changes are ignored. A new tap resets the
  baseline.
- **Quiet hours 11 PM–9 AM Pacific:** nothing is sent and the baseline does
  not move. At 9 AM one catch-up lists games that moved a full point, with
  the overnight path read from the forwarder's `nba_line_snapshots`.
- **Picker updates:** one message when Trapp's or Cowboy's stance on a
  baited game changes before tip.
- **Book divergence:** see below; only for baited games.
- Every unprompted message is recorded as sent **before** sending and never
  retried (see `telegram-bot-api.md`: a delivered message can still time
  out, which once repeated one alert six times).

## Line sources

- Bovada via the forwarder's `nba_games` Sheet tab (see
  `sports-data-sources.md`). Rows matched to that tab are marked Bovada.
- A game with **no row** (Bovada hasn't posted it) gets BetOnline's number
  from the last scheduled Odds API read (00:00, 15:00 and 20:00 Pacific;
  `spreads,h2h`, 2 credits; a missed slot is read at start-up). It is labelled
  "· BetOnline" and replaced once Bovada posts.
- A game whose row has **no lines** was pulled (usually injury news): it
  shows "off the board", gets no buttons and no alerts, and never takes the
  older BetOnline number.
- **Three-book check:** 60 minutes before every NBA tip, one call with
  `bookmakers=betonlineag,bovada,hardrockbet&markets=spreads` (1 credit)
  stores all three in `book_checks`. A divergence is BetOnline a full point
  off Bovada or Hard Rock with that side's juice within 2.5 points of
  implied probability (a bigger juice gap can make a different number the
  same price). BetOnline's side is graded at the softer number and at its
  own.
- The Odds API key comes from the forwarder's guesser env; the installer
  copies it into Bell's env as `DECISION_HELPER_ODDS_API_KEY`. Without it
  both Odds API features are off.

## Operating it

- Install/update: the Bell installer through `systemd-run` (command in
  `www/CLAUDE.md`); it runs the tests, restarts the service, and needs
  `auth.env` loaded (see `claude-code-auth.md`).
- Retrigger a line alert: set the game's `bait_watch.next_check_at` to now
  and `alert_line` to NULL; the 1-minute `bait-checks` job sends it if the
  move still qualifies.
- Remove a mis-tapped bait: the user's ↩️ Undo & re-pick button (latest row
  only, before tip). By hand: delete the row and its `bait_messages`, then
  clear `alert_line`.
- Rename the bot: `setMyName` via the Bot API (rate-limited; do it once).
  The username only changes in BotFather and the config checks it.

## Reading bait stats

- Re-takes on one game are correlated: every take on a game shares its
  result. Report records per game (final position) and per take separately;
  a re-take multiplies a result rather than changing it.
- Group re-takes by why the number changed: cheaper because the market moved
  away from the side, worse because the user chased it, or a switch of
  sides. Name the book; a switch between BetOnline and Bovada numbers is not
  a market move.

*Connections:* nba-picker-tracking.md, sports-data-sources.md,
youtube-pick-extraction.md, telegram-bot-api.md, claude-code-auth.md,
capper-backtesting.md.
