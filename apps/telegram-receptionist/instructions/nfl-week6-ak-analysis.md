# Add AK to the 2026 NFL Week 6 picker-history reports

Use this runbook after AK has submitted a pick and predicted score for every
2026 NFL Week 6 game. Generate the same per-matchup analysis and PNG reports
that already contain the Doc's Sports, Ringer Wise Guys, and Sharp or Square
pickers, with AK shown as an additional picker rather than in a separate report.

## Existing report assets

The existing reports and other-picker data are committed beside this runbook:

```text
/home/receptionist/repos/www/apps/telegram-receptionist/instructions/nfl-week6-report-assets/
```

Use:

- `base-reports.json` as the frozen machine-readable baseline for all 14
  reports and all existing picker rows;
- `baseline-pngs/` as the visual reference for the reports already delivered;
- `render_week6_reports.py` to render the AK-enriched JSON; and
- `README.md` for the enrichment schema and renderer command.

Do not require `/home/forwarder/data/nfl_audio_parser.sqlite3` to recover the
other pickers. It was not populated with the historical artifacts used to
build these reports. Preserve the baseline rows and calculations from
`base-reports.json`; use the authoritative intake data only for AK's picks,
predicted scores, revisions, and results.

## Required outcome

For every Week 6 matchup:

1. Preserve the existing picker positions and historical calculations.
2. Add AK's latest pregame Week 6 position to the current board and table.
3. Analyze AK's completed picks from Weeks 1 through 5, plus any completed
   Week 6 games, involving either team in the matchup.
4. Report both:
   - how the selected team performed ATS at AK's stated spread; and
   - how each team scored relative to AK's predicted score.
5. Update the notable points and expectation without treating incompatible
   contracts as equivalent.
6. Render one full-resolution PNG per matchup. If asked to deliver them, send
   each as a separate Telegram document with no caption.

Do not create an AK-only report. AK must appear alongside the other pickers in
the existing Week 6 matchup report.

## Authoritative inputs

Work from the production forwarder account when reading the authoritative NFL
intake data:

```bash
sudo -n -u forwarder -H bash -lc \
  'cd /home/forwarder/app && /home/forwarder/venv/bin/python <command-or-script>'
```

Use the production checkout's own schema/constants to locate:

- AK's submitted NFL picks and every revision;
- AK's predicted away and home scores;
- the spread and price captured with each pick;
- confidence or ranking, when submitted;
- the `nfl_games` schedule rows and final scores; and
- stable event IDs, season, week, and kickoff time.

Inspect the production schema before querying. Do not guess worksheet names,
column names, database tables, team aliases, or score orientation. Never print
credentials, bot tokens, recipient IDs, or private environment values.

Use canonical picker name `AK`. Normalize only aliases that the authoritative
data explicitly maps to AK. Do not merge another person's picks based on
similar initials.

The `nfl-history` skill is limited to completed 2023-2025 games and is not an
authoritative source for this 2026 slate. Do not use it, remembered scores,
prose, or a web scoreboard as a substitute for the 2026 intake game's final
score. If an authoritative 2026 final is missing, mark that game unresolved.

The production celebrity-picker opinion store, if separately needed, is:

```text
/home/forwarder/data/nfl_audio_parser.sqlite3
```

Query it read-only unless the task explicitly requires an approved ingestion or
grading operation. Its absence is not a blocker because the reviewed
other-picker baseline is committed in `base-reports.json`.

## Completeness checks before analysis

Do not render final reports until all checks pass:

- Exactly one latest pregame AK submission exists for each Week 6 event.
- Every AK Week 6 submission identifies a side or explicitly records a pass.
- Every non-pass submission has the spread AK actually picked, or is visibly
  marked ATS-ungradeable.
- Every Week 6 game has both predicted team scores, or the missing field is
  visibly marked unavailable.
- Every Week 1-5 AK row used in history predates kickoff.
- Every historical game used for grading has a final score.
- Revisions are collapsed to the latest valid pregame submission; repeated,
  unchanged submissions do not create extra picks.
- Team names resolve to the authoritative event's away and home teams.

If Week 6 submissions are incomplete, state exactly which games or fields are
missing and stop. Do not silently omit a game or fill a missing prediction.

## Canonical per-game calculation

For each completed AK submission, retain these fields in a structured
intermediate dataset:

```text
event_id
season
week
kickoff
away_team
home_team
picked_team
opponent
picked_spread
predicted_away_score
predicted_home_score
actual_away_score
actual_home_score
confidence
submission_timestamp
revision_identifier
```

Orient every calculation explicitly; never infer orientation from row order.
Let:

```text
actual_team_points       = final points scored by the team being analyzed
predicted_team_points    = AK's predicted points for that team
actual_opponent_points   = final points scored by its opponent
predicted_opponent_points = AK's predicted points for its opponent

team_score_delta = actual_team_points - predicted_team_points
opponent_score_delta = actual_opponent_points - predicted_opponent_points
actual_margin = actual_team_points - actual_opponent_points
predicted_margin = predicted_team_points - predicted_opponent_points
margin_delta = actual_margin - predicted_margin
actual_total = actual_team_points + actual_opponent_points
predicted_total = predicted_team_points + predicted_opponent_points
total_delta = actual_total - predicted_total
```

Interpret signed deltas as follows:

- positive `team_score_delta`: the team scored more than AK predicted;
- negative `team_score_delta`: the team scored fewer than AK predicted;
- positive `margin_delta`: the team performed better relative to its opponent
  than AK's score projection;
- negative `margin_delta`: the team performed worse than projected;
- positive `total_delta`: the game finished over AK's predicted total; and
- negative `total_delta`: the game finished under AK's predicted total.

Also retain absolute errors:

```text
team_score_absolute_error = abs(team_score_delta)
opponent_score_absolute_error = abs(opponent_score_delta)
margin_absolute_error = abs(margin_delta)
total_absolute_error = abs(total_delta)
```

### ATS grading

Grade only the spread AK submitted:

```text
picked_team_margin = picked_team_points - opponent_points
ats_value = picked_team_margin + picked_spread
```

- `ats_value > 0`: ATS win.
- `ats_value < 0`: ATS loss.
- `ats_value == 0`: push.

Use the line exactly as stated at submission. Do not replace it with a closing
line, a current line, another picker's line, or a later post-kickoff line. If
AK submitted moneyline only, grade the moneyline separately and mark ATS as
ungradeable. Keep teasers, adjusted spreads, parlays, first-half bets, and
straight full-game spreads as distinct contracts.

For both teams in each current Week 6 matchup, classify each historical AK pick
as one of:

- **backing team**: AK selected that team;
- **fading team**: AK selected its opponent;
- **not an ATS position**: moneyline-only, pass, missing line, or another
  period/market.

The same historical game may supply a "backing Team A" observation and a
"fading Team B" observation, but it is still one AK contract and must not be
double-counted in AK's overall record.

## Historical splits for each Week 6 matchup

Use completed Weeks 1-5, and completed Week 6 games when applicable. For each
of the two teams, calculate:

- AK ATS record when backing the team: `W-L-P`;
- AK ATS record when fading the team: `W-L-P`;
- combined AK ATS record in games involving the team, counting each AK
  contract once;
- average and median team score delta when AK backed the team;
- average and median team score delta when AK faded the team;
- counts of the team scoring over, under, or exactly at AK's prediction;
- average margin delta when AK backed and faded the team;
- average total delta and mean absolute total error;
- favorite/underdog split using AK's stated spread;
- confidence split when confidence exists; and
- sample size for every split.

Use `W-L-P`, not only a percentage. If a percentage is shown, exclude pushes
from the denominator and show the underlying sample. Do not describe a one- or
two-game sample as a reliable tendency.

For the matchup-level AK row, show:

- current Week 6 pick, line, confidence, and predicted score;
- prior ATS record in games involving either team;
- backing/fading records for each team;
- each team's over/under-versus-AK-prediction counts;
- average signed team score deltas;
- average signed margin delta; and
- a short interpretation that includes sample size.

Example interpretation shape:

```text
AK is 3-1 ATS in four prior games involving these teams: 2-0 backing Team A
and 1-1 fading Team B. Team A scored over AK's projection twice and under once
(average +3.7 points); Team B averaged 2.0 points below projection. AK's
projected margin missed by 4.3 points on average. Small sample.
```

Do not use "overperformed" or "underperformed" without naming the reference:
points versus AK's team-score prediction, margin versus AK's predicted margin,
or ATS versus AK's submitted spread.

## Add AK to the existing report

Copy `base-reports.json` to a working AK-enriched JSON file. Keep each existing
report title, other picker row, source caveat, market-timing warning, and
completed-game grading. Add AK as a normal row in the picker table, preferably
after the existing source-grouped rows unless the report has an established
ordering. Do not edit the committed baseline file in place.

The AK row should use these columns:

| Column | AK content |
| --- | --- |
| Person | `AK` |
| Week 6 position | Side, exact spread/contract, confidence, predicted score |
| Prior graded record | ATS `W-L-P`, with unresolved count if needed |
| Confidence / calibration | Confidence split plus concise score-error summary |
| Interpretation | Backing/fading and predicted-score tendencies for both teams |

Add an **AK score calibration** block below the table when the row cannot hold
all required detail. Include one compact line per team:

```text
Team A — backed: 2-0 ATS, score vs prediction 2 over/1 under, avg +3.7;
faded: 1-1 ATS, 1 over/1 under, avg -0.5.
```

Update the report's board structure and expectation to mention AK, but preserve
contract distinctions:

- A straight spread is not equivalent to a teaser or moneyline.
- Different sides at numbers separated by key values may both be rational.
- A stale line must not be counted as current consensus.
- A conditional pick remains conditional.
- Opposing positions can form a middle rather than a true reversal.

When summarizing agreement, report the straight-spread headcount separately
from teasers, moneylines, parlays, and period bets. Include AK in a consensus
count only when AK's contract is comparable.

For a completed game, prominently show the final score and grade AK's original
pregame contract. Do not rewrite the report as if the result were unknown.

## Validation

Before delivery, create a machine-readable audit file containing every AK
historical game and all formulas' inputs and outputs. Verify:

1. one latest valid pregame AK contract per event/market thread;
2. ATS records reconcile to the game-level grades;
3. over/under/equal counts reconcile to score deltas;
4. averages use the displayed sample and signed values;
5. away/home score orientation is correct;
6. Week 6 unplayed games are not included in historical records;
7. the completed Seahawks-Broncos game is included and graded if its AK pick
   was submitted before kickoff;
8. no other picker's historical numbers changed unexpectedly; and
9. every PNG is visually inspected for clipped cells, missing footnotes, and
   wrapped calibration text.

Also compare every newly rendered report with the matching file in
`baseline-pngs/` and confirm that any changed non-AK prose is necessary to
describe AK's effect on the board. Existing picker records must not drift.

Where source data is incomplete, write `unavailable` or `unresolved` and name
the missing field. Never turn missing data into `0-0`, zero score error, a pass,
or an implied pick.

## Suggested prompt

After all AK picks are submitted, point the receptionist to this file with:

```text
Read and follow
/home/receptionist/repos/www/apps/telegram-receptionist/instructions/nfl-week6-ak-analysis.md
to add AK to every 2026 NFL Week 6 picker-history report. First verify that all
Week 6 AK picks and predicted scores are complete. Generate the structured
audit data and updated PNGs using the committed assets in
/home/receptionist/repos/www/apps/telegram-receptionist/instructions/nfl-week6-report-assets/,
visually inspect them, and report any missing or unresolved AK source data
instead of guessing.
```
