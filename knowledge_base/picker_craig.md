# Picker: Craig Trapp (Doc's Sports)

Verified: 2026-11-23 (2026 NFL weeks 1–11, Monday night of week 11 not
graded; 2026-27 NBA Oct 20 – Nov 22).

## Who he is

Craig Trapp is a Doc's Sports handicapper who co-hosts free YouTube picks
shows with Indian Cowboy (see `picker_indiancowboy.md`) and sells paid
"unit" plays through Doc's Sports. He hosts: he opens each show, reads the
lines and asks Indian Cowboy for his pick. On air he calls himself a Bengals
fan. Transcripts often mishear the pair's names ("Craig Arum", "Craig
Aron").

## Where his picks come from

- **Sunday show** ("NFL Week N Picks - Sunday … Craig Trapp & Indian
  Cowboy"), posted Thursday or Friday. The two hosts alternate games, so
  each Sunday game has one picker. Covers the 1 PM and 4 PM games.
- **Primetime shows** for Thursday, Sunday and Monday night, posted the day
  before or the morning of the game. Usually both hosts pick; some weeks
  Craig posts the Sunday or Monday night show alone.
- He also gives player props and names his paid top plays only as "the side"
  or "the total"; those paid plays are never revealed, so they are not
  tracked.

## What we tracked and how

Pipeline in `youtube-pick-extraction.md`; grading and tests in
`capper-backtesting.md`; lines from `sports-data-sources.md`. Per pick:
show, picker, game, market, selection, line as spoken, pick vs lean, an
exact quote, kickoff window, DraftKings close and result at the close.
Working files live in the agent's `~/craig-ic-nfl` (transcripts, extracted
picks, grading and backtest scripts, a per-week live-bet ledger).

## Record (graded at the DraftKings close, sides and totals)

- Overall **41-61**; weeks 1–4 18-22, weeks 5+ 23-39. Spreads 24-35,
  moneylines 1-7, totals 16-19 (overs 5-10, unders 11-9).
- **By show:** Sunday show 24-48 (both halves bad); Thursday 6-7; Sunday
  night 8-4; Monday night 2-2.
- **By kickoff window (Sunday show):** 1 PM 18-26 (sides 14-14 through week
  10); **4 PM 6-19**, bad in both halves (4-7, then 2-12). This was the
  strongest fade we found (p ≈ 0.035 on weeks 1–10).
- **By situation:** small underdogs (3.5 or less) 5-14; home sides 9-22
  (2-15 since week 5); home underdogs 4-13; divisional sides 6-17 (0-5,
  then 6-12); overs 2-7 since week 5. Road underdogs getting 4–6.5 were
  5-1, the one good spot.
- **Line movement** after his show means nothing for him (moved his way
  14-16, flat 17-19, against 6-12).

## What we learned

- Fading his late-Sunday (4 PM) picks held all season; fading his 1 PM
  picks did not. Earlier ideas (season-to-date group tiers, streaks,
  using 1 PM results to change 4 PM bets) failed walk-forward tests.
- He repeatedly backed the same struggling teams (Chargers 0-4, Falcons
  2-5), so team-specific blind spots are worth watching.
- Several fade angles overlap on the same pick; treat them as one signal.
- All records are in-sample from roughly 60 splits tested; only the 4 PM
  fade and the overall Sunday fade have much statistical weight.

## NBA

- **Source:** the daily free show "<Day> NBA Picks … Craig Trapp & Indian
  Cowboy", posted roughly midnight–2 AM ET on game day and parsed
  automatically (`nba-picker-tracking.md`). Some days it is Craig-only
  ("… Craig Trapp NBA Picks"). He gives a side or total on most games, says
  "small play" or "lean" for weaker ones, occasionally picks a first half,
  and sometimes makes a pick conditional on a player playing.
- **Record at the DraftKings close** (graded with the `~/craig-ic-nba`
  scripts): sides 55-62, totals 18-17.
- **Held in both season halves** (Oct 20 – Nov 4 vs Nov 5 on):
  - Sundays **20-7** (p ≈ 0.02); every other day 53-72. Only five Sundays,
    one of them 7-1.
  - Medium favourites (4.5–9.5 at the close) 11-17; road favourites 16-21;
    favourites whose line moved toward him after the show 10-18.
- **Did not hold:** "line moved against him" (a better number for the
  bettor) was 18-8 but almost all of it came in the second half; small
  dogs, unders and home dogs flipped between halves.
- **Teams:** backed Memphis 7-2. Per-team samples are 1–9 picks.
- **NBA vs NFL Sundays:** his NBA Sundays are his best day while his NFL
  Sunday show is his worst; NFL Sunday night (8-4 at the close) is the
  exception.
- **In practice** daily tail/fade calls built from these splits went badly
  (the best-supported pick lost on consecutive days; one day's three calls
  went 0-3), consistent with most splits being noise.

*Connections:* picker_indiancowboy.md, youtube-pick-extraction.md,
capper-backtesting.md, sports-data-sources.md, nba-picker-tracking.md.
