---
name: nfl-close-picks
description: Deterministic straight-up winner picks for 2026 NFL games with a spread of 2.5 or less (close games), with a curve-fit pick and an honest walk-forward pick per game, plus the season-to-date record. Use when the user asks for close-game NFL picks, "spread <= 2.5 winners", this week's close games, or how the close-game models are doing.
---

# NFL close-game picks (|spread| <= 2.5, winner only)

Repository: `/home/receptionist/repos/nfl-close-picks` (run every command from
there). Data = spread/total lines (Sheet snapshots, Bovada since 2026-10-27)
and ESPN finals only.

## When to use

- "Close game picks this week", "who wins the games with spread under 3",
  "pick the 2.5-or-less games", "NFL picks week N close games".
- "How are the close-game picks doing" -> record command.
- Not for spreads, totals, props or parlays: these models pick the
  straight-up winner only.

## Commands

1. Picks for the current week (pulls the latest Sheet lines and ESPN finals
   first; Sheet credentials come from the environment, never print them):

   ```bash
   cd /home/receptionist/repos/nfl-close-picks
   .venv/bin/python scripts/predict.py --refresh
   ```

   Options: `--week N` (default = earliest 2026 week with unplayed games),
   `--json`, `--max-spread 2.5`, `--frozen` (curve-fit uses the original
   W1-8 params `full.json` instead of `full_live.json`). Without `--refresh`
   it uses the cached `data/processed/games_2026.csv`.

2. Season-to-date record (all numbers via `ncp.evaluate`, include_holdout):

   ```bash
   .venv/bin/python scripts/predict.py --record
   ```

3. Weekly refit of the curve-fit after a week's games are final (about 2 s,
   seed 0, deterministic), then re-run the picks:

   ```bash
   .venv/bin/python scripts/build_data.py --2026 --refresh
   .venv/bin/python scripts/refit_curvefit.py
   .venv/bin/python scripts/predict.py
   ```

   It refits the 14-parameter formula on ALL completed 2026 close games and
   writes `src/ncp/track_d/params/full_live.json` (weeks fitted, n games,
   in-sample accuracy, seed, date). Commit the new file.

## What each pick means (say this to the user, verbatim caveats)

- Curve-fit pick (`track_d_curvefit`): "in-sample fit: 90% on 2026 W1-8;
  expect ~50% on new games". (With the live params the label shows the
  current fit span, e.g. "90% on 2026 W1-10".) It is the user's
  "implied score x magic multipliers x team ratings" formula fit to this
  season's results. Cross-validated it is 50%, on 2015-2025 it is 50.5%, and
  a search on randomly shuffled results reaches ~88% too: the 90% is overfit,
  not skill. It may say SKIP when its predicted margin is below its fitted
  threshold.
- Honest pick (`track_e_final`): "walk-forward best: ~market favourite
  level". It is the no-vig moneyline favourite and skips the games closest to
  50/50 (at most 20% of the season's close games). Market favourites win
  about 53% of close games (2015-2025), 57-60% (2023-2025), ~50% in 2026.
- Neither the 80% target nor the 65% fallback is reachable with spread and
  total data alone; no walk-forward model beat the market favourite.
- Lines marked "provisional" are the latest pre-kickoff snapshot and can still
  move; a game whose line moves outside 2.5 drops out of the list. Re-run
  with `--refresh` closer to kickoff.

## Output rules

- The user reads on Telegram: paste the script's bullet output, no markdown
  tables. Keep both picks and both labels for every game.
- Do not present the curve-fit percentage without "in-sample" next to it.
- A game result marked "(in-sample: game was in the fit set)" is not evidence
  for the curve-fit.
