# 2026 NFL Week 6 report assets

This directory is the portable baseline for
`../nfl-week6-ak-analysis.md`.

- `base-reports.json` contains all 14 reviewed matchup reports and every
  existing picker row.
- `baseline-pngs/` contains the 14 reports delivered before AK was added.
- `render_week6_reports.py` renders baseline or AK-enriched report JSON.
- `requirements.txt` declares the renderer's only third-party dependency.

The baseline JSON is the source of truth for the existing Doc's Sports, Ringer
Wise Guys, and Sharp or Square rows. Do not try to reconstruct those rows from
the production opinion database. That database was not populated with the
historical artifacts used for these reports.

## AK-enriched JSON

Copy `base-reports.json` to a working file. For each report:

1. Add one row to `rows` with `speaker` equal to `AK`.
2. Add an `ak_calibration` array containing the compact team-by-team score
   calibration lines required by the runbook.
3. Update `intro`, `favorite_records`, `notable`, `expectation`, and `footnote`
   only as needed to include AK. Preserve the meaning and numbers of every
   baseline picker row.

An AK row has the same shape as every other row:

```json
{
  "speaker": "AK",
  "position": "Team +3, high; predicted Away 24, Home 21",
  "overall_text": "3-2 ATS",
  "confidence_text": "High 2-1; score MAE 4.2",
  "interpretation": "Backing/fading and score-calibration summary."
}
```

The optional calibration block is:

```json
{
  "ak_calibration": [
    "Team A - backed: 2-0 ATS; score vs prediction 2 over/1 under; avg +3.7.",
    "Team B - faded: 1-1 ATS; score vs prediction 1 over/1 under; avg -0.5."
  ]
}
```

## Render

From this directory:

```bash
python3 -m venv /tmp/nfl-week6-report-venv
/tmp/nfl-week6-report-venv/bin/pip install -r requirements.txt
/tmp/nfl-week6-report-venv/bin/python render_week6_reports.py \
  --input /path/to/ak-enriched-reports.json \
  --output-dir /path/to/output
```

Use `--only <slug>` repeatedly to render selected matchups. The renderer
supports DejaVu Sans on Linux and Arial/Helvetica on macOS.
