# Backtesting a pick-seller's "edges"

Verified: 2026-11-23.

Method for checking whether patterns in a handicapper's record are real.
Learned on two NFL hosts over ten weeks (about 190 graded sides and totals);
most patterns that looked strong did not survive these checks.

## Grade fairly

- Grade at the closing line, not the number said on air. Shows are recorded
  days early and sometimes quote an off-market number, which inflates the
  record.
- Separate picks from leans, and sides from totals and props.

## Test like a forecaster

- **Walk forward:** score each pick using only games that kicked off before
  it. A rule built from season-to-date group records ("play groups above 55%,
  fade groups below 45%") scored worse than a coin flip this way, even
  though the groups looked strong in hindsight.
- **Split the season:** compare early weeks with later weeks. Patterns that
  hold in both halves are worth tracking; many died after week 4.
- **Shuffle test:** to ask whether recent form carries over (last week's
  group record, or the 1 PM window predicting the 4 PM window), shuffle
  results within each group and see how often a gap as large appears by
  chance. None of the carryover ideas tested beat chance.
- **Replicate on a second book:** a 19-3 "line moved toward his pick" edge
  existed only against one book's close and was 15-12 against another's.
  About a third of its "moves" were the host quoting an off-market number,
  not the market moving.

## Interpret carefully

- Testing about 60 splits will produce a few 30% or 70% groups by chance.
  Only trust ones with a reason, both halves agreeing, and a second-book
  check.
- Several angles often point at the same pick; they are not independent
  edges and should not be stacked.
- Turn records into probabilities by pulling them toward 50% (for example
  adding 10–20 imaginary games at 50%); -110 pricing needs 52.4% to break
  even.
- Before blaming injuries for a dying trend, check league-wide shifts (all
  home underdogs, all overs in the same weeks), starting-QB changes from box
  scores, and team form. Team against-the-spread form over the last four
  games predicted nothing league-wide.
- Line moves are concentrated late (most in the final 24 hours), and closes
  can move in the last minutes, so qualify any line-based rule at kickoff.

## Lessons from repeating it on NBA picks

- **Bucket by the closing number.** Sizing spreads by the number said on
  air and by the close gave opposite answers for the same host (small
  favourites 6-9 vs 13-9), because dogs on air often close as favourites.
- **Re-test before carrying an edge over.** The NFL line-movement edge
  swung from losing to winning between halves on the same host's NBA picks
  (10-14, then 16-5), so it is not established there.
- **Recompute surprising splits independently.** A "picks against team X"
  filter that matched every pick in X's games produced plausible but wrong
  numbers until checked against a second calculation.
- **Ranking a day's picks by how many splits agree is stacking.** Used
  live for several days, the "best-supported" picks lost about as often as
  they won.

*Connections:* sports-data-sources.md, youtube-pick-extraction.md,
picker_craig.md, picker_indiancowboy.md (worked examples),
nba-picker-tracking.md.
