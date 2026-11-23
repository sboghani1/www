# Picker: Indian Cowboy (Doc's Sports)

Verified: 2026-11-23 (2026 NFL weeks 1–11, Monday night of week 11 not
graded; 2026-27 NBA Oct 20 – Nov 22).

## Who he is

Indian Cowboy ("IC"; Craig calls him "Arun") is a Doc's Sports handicapper
who co-hosts the free NFL picks shows with Craig Trapp (see
`picker_craig.md`). He also runs his own channel content under the Indian
Cowboy name, including daily free-pick videos and members-only "YouTube
Premium" videos, and says he uses statistical models (biostatistics
background). On air he calls himself a Falcons fan, which helps tell the
speakers apart.

## Where his picks come from

- **Joint Sunday show**: he and Craig alternate games, one picker per game.
- **Joint primetime shows** (Thursday, Sunday, Monday night): usually a side
  or total from each host.
- **Solo free videos** (for example "Monday NFL Picks … Indian Cowboy
  Picks"): sometimes a different pick from his joint-show pick for the same
  game. These were logged separately and are **not** part of the records
  below. "YouTube Premium" videos are members-only and cannot be fetched.

## What we tracked and how

Same pipeline and fields as for Craig (`youtube-pick-extraction.md`,
`capper-backtesting.md`). For Indian Cowboy we also tracked, per pick, how
the DraftKings line moved from the number he said on air to the close, and
ran a forward "live-bet" test on picks where it moved his way.

## Record (graded at the DraftKings close, sides and totals)

- Overall **55-40-4**; weeks 1–4 22-13-4, weeks 5+ 33-27. Spreads 33-23-4,
  moneylines 5-4, totals 17-13 (overs 9-9, unders 8-4).
- **By show:** Sunday show 43-27-3 (good in both halves); Thursday 4-7;
  Sunday night 5-1; Monday night 3-5. Primetime overall faded after week 4.
- **By kickoff window (Sunday show):** 1 PM 28-20-1; 4 PM 12-6-2.
- **By situation:** underdogs getting 4–6.5 12-4-1 (road 8-1-1); divisional
  sides 14-8 (9-4 since week 5); home sides 19-11-1; home underdogs 7-3;
  small underdogs 5-6-1; overs 4-7 since week 5.
- **Line movement (DraftKings):** moved at least half a point his way
  **22-4-1**; no move 8-18; moved against him 20-14-3.

## What we learned

- The "moved his way" record only holds against DraftKings' close. Against
  the forwarder's BetOnline/Bovada history the same test was about 15-12,
  and about a third of the DraftKings "moves" were him quoting a number
  that was already off the market. It is tracked as a forward test, acted on
  only as a live bet after kickoff, because the qualifier is the closing
  line. Lines often moved in the last minutes before kickoff.
- His underdogs of 4–6.5 and his divisional sides held in both halves; his
  overs and primetime picks faded after the first month.
- Unlike Craig, his record does not support a blanket fade; his "no move"
  record (8-18) did not replicate on a second book either.
- All records are in-sample from many splits; treat them as leans.

## NBA

- **Sources:** the joint daily show with Craig, plus solo free videos
  ("<Day> NBA Picks and Predictions, Indian Cowboy Picks …"). The NBA intake
  stores the solo videos as a separate program ("Doc's Sports Indian Cowboy
  NBA Picks", from Oct 21), but the exported picks and the records below
  **combine both**, unlike his NFL records. Some days he appears on neither
  free video; members-only "YouTube Premium" videos cannot be fetched.
- **Record at the DraftKings close:** sides 24-31, totals 28-25.
- **Road favourites:** 6-3 at the close (6-1 at his own number). Five of the
  six wins were by 16 or more, so line moves rarely mattered. Held in both
  halves but only nine picks.
- **Home sides** 7-19 (p ≈ 0.03), but 3-15 in the first half and 4-4 since,
  so it may be fading. **Overs flipped:** 4-9, then 11-4.
- **The NFL "line moved his way" edge is unproven on NBA picks:** graded at
  the close those picks are 26-19, but 10-14 in the first half and 16-5
  since, so it has not held in both halves and has not been checked on a
  second book. NBA shows are recorded the night before, and large moves are
  usually injury news.

*Connections:* picker_craig.md, youtube-pick-extraction.md,
capper-backtesting.md, sports-data-sources.md, nba-picker-tracking.md.
