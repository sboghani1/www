# NBA picker tracking (Craig Trapp and Indian Cowboy)

Verified: 2026-11-23.

How to answer day-to-day questions about the Doc's Sports hosts' NBA picks:
what they picked, how they have done, and how today's picks fit their
trends and the user's bait. Pick parsing is in `youtube-pick-extraction.md`;
the hosts' records are in `picker_craig.md` and `picker_indiancowboy.md`.

## Where the picks are

- **Today's picks with quotes:** query `current_opinion_observations` in the
  forwarder opinion store as the `forwarder` account, read-only
  (`file:…?mode=ro`), filtered by `sport = 'nba'` and `valid_from_date`.
  Columns include `picker_name`, `selection`, `conviction_label`,
  `opinion_status`, `condition_text`, `period` and `evidence_json`.
- **All latest picks:** `/var/lib/decision-helper-bot/picker-picks.json`
  (owned by the agent account, mode 600). Full-game sides and totals only,
  one side per game per picker.
- **Did today's show parse?** `nba-channel-state.json` (status per video,
  keyed by hash, with `slate_date` and `observations`), the intake's
  journal (`journalctl -u nba-channel-intake.service`), and a coverage check
  against the channel listing's duration.
- **Nothing grades the hosts' own picks.** The store's `opinion_grades` is
  empty for them; Bell's `picker_stats` only grades the user's bait split by
  where each host stood.

## Scripts (`~/craig-ic-nba/scripts`, agent account)

Copy the export to `~/craig-ic-nba/picks.json` first. Run with `python3 -I`.

- `grade.py picks.json espn_cache.json graded.json` — grades every pick
  against ESPN finals at the number said on air, or ESPN's line when none
  was said. Caches finished games. Conditional picks count only when the
  condition was met; conditions it cannot parse are listed by hand in
  `conditions.json` (`"picker|event_id|selection": true/false`).
- `linemove.py` — the number said on air vs the DraftKings close, grouped
  toward / flat / against, graded at both numbers.
- `close_trends.py` — re-grades everything at the close, sizes spreads by
  the closing number, and prints each split with first-half and second-half
  records, flagging those that held in both.
- `today_card.py YYYYMMDD` — scores one day's picks against current
  DraftKings lines (closing lines for games already started) using the
  close-graded history.
- `tx_local.py in.wav out.txt` — local faster-whisper transcript for fuller
  quotes than the store keeps.

## Comparing with the user's bait

- How Bell stores bait (rows per tap, current position, book, passes) is in
  `bell-bait-tracker.md`.
- `decision_helper.pickers.picker_stats(baits, PickerFeed(path), tz)` (run
  with Bell's installed venv) gives the user's record with each host on the
  same side, the opposite side, or no side.
- "Has the line moved since my bait?": take each game's highest-`ordinal`
  row, compare its `line` with the game's `latest_*` cell in `nba_games`,
  and read the path from `nba_line_snapshots` (rows only on change). Match
  games by team nicknames and tip time: the tabs keep past games, so a name
  match alone can pick an earlier meeting of the same teams.

## Answering well

- Always say which book and when a line was read, and whether a game has
  started.
- Records rest on 1–30 picks per split. Present them as leans, flag
  conflicting splits, and say when earlier recommendations missed.
- Check injury news (`sports-data-sources.md`) before reading anything into
  a line move of two points or more.

*Connections:* picker_craig.md, picker_indiancowboy.md,
youtube-pick-extraction.md, sports-data-sources.md, capper-backtesting.md,
bell-bait-tracker.md.
