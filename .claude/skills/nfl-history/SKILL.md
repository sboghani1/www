---
name: nfl-history
description: Answer analytical questions about completed 2023-2025 NFL games, scores, home records, weeks, conferences, and divisional meetings from the cached nfl_game_history Sheet tab.
---

# NFL game history

Use this skill whenever the user asks an analytical question about completed
NFL games from the 2023, 2024, or 2025 regular seasons.

## Source and safety

- Query only the `nfl_game_history` data exposed by
  `/usr/local/bin/receptionist-nfl-history-helper`.
- Do not fetch ESPN, browse the web, inspect `nfl_games`, or infer results from
  prose.
- The helper is read-only. Never modify the Google Sheet or cache manually.
- Treat structured columns as authoritative. Use `matchup_type`,
  `division_meeting_number`, `week`, `neutral_site`, and `overtime` rather than
  parsing `tags`.

## Query workflow

1. Run:

   ```bash
   /usr/local/bin/receptionist-nfl-history-helper schema
   ```

   This creates the private 816-row SQLite cache from the Sheet when missing
   and otherwise reuses it without a network request.

2. Translate the user's question into one aggregate SQLite query against the
   `games` table:

   ```bash
   /usr/local/bin/receptionist-nfl-history-helper query --sql '<SELECT query>'
   ```

   Only `SELECT` and `WITH` statements are accepted. Prefer conditional
   aggregation so all comparison groups come from one consistent query. Use
   one statement without SQL comments or semicolons inside string literals.

3. State the exact filters, sample size, and result. For records, report
   `W-L-T`; when reporting winning percentage, use
   `(wins + 0.5 * ties) / games`.

4. For a home-field question, exclude `neutral_site = 1` by default and state
   that choice. If neutral-site games materially affect the requested group,
   report them separately.

5. Run `refresh` only when the user explicitly asks to refresh from Google
   Sheets or says historical rows were corrected:

   ```bash
   /usr/local/bin/receptionist-nfl-history-helper refresh
   ```

## Field semantics

- `matchup_type = 'division'`: same division.
- `matchup_type = 'conference'`: same conference, different divisions.
- `matchup_type = 'non_conference'`: AFC versus NFC.
- `division_meeting_number`: chronological meeting 1 or 2 for the unordered
  divisional opponent pair in that season; blank for non-divisional games.
- `home_result`: `W`, `L`, or `T` from the listed home team's perspective.
- Boolean fields are SQLite integers: `0` or `1`.

Do not answer from remembered results. Every answer must come from a helper
query, even when a prior conversation contains a similar calculation.
If the helper returns `ok: false` or exits unsuccessfully, report its error and
stop. Never fall back to memory, another Sheet tab, ESPN, or the web.
