# Extracting picks from YouTube picks shows

Verified: 2026-11-23.

How a free YouTube picks show becomes a list of graded, checkable picks.
Built for Doc's Sports NFL shows; the same steps fit any talk-format show.

## Listing and downloading

- List the channel's uploads with yt-dlp `extract_flat: "in_playlist"`
  (no download, works from the VPS directly). Set `playlistend` deep enough
  to reach the start of the season: a busy multi-sport channel needed about
  3,000 entries to reach August.
- Downloads from the VPS address are bot-blocked by YouTube; download through
  the proxy configured for the nfl-audio-parser environment
  (`NBA_CHANNEL_PROXY`, never printed). `nfl_audio_parser.nba_channel`
  exposes `download_audio` and `check_complete` (rejects audio under 95% of
  the reported length; uploads fetched too soon can be truncated).
- "Premium"/members-only videos cannot be fetched.

## Transcribing

- `nfl_audio_parser.transcribe.transcribe` sends the audio to AssemblyAI with
  speaker labels and returns utterances (`speaker_label`,
  `source_start_seconds`, `text`). Delete the provider transcript afterwards
  (`DELETE /transcript/<id>`), as the intake does.
- Speaker letters are per video. Identify hosts from context: who opens the
  show, who is addressed by name before answering, fan remarks ("my
  Falcons"). Transcripts mishear names, so match loosely.

## Extracting picks

- Run parallel subagents over batches of transcripts with a fixed JSON
  schema: picker, teams, market, selection, line as spoken, stated favourite,
  strength (pick, lean, best bet), and an exact quote.
- Check every quote is a verbatim substring of its transcript before using
  the picks.
- Record leans ("if you made me pick…") separately from picks. Paid "unit"
  plays mentioned but not named are not picks.
- Cross-check against any independent extraction where the two overlap (the
  forwarder opinion store held two weeks of the same hosts): sides and totals
  matched 13/13, and the comparison exposed one store "lean" that was not a
  real pick.
- Keep joint shows and a host's solo videos as separate sources; do not mix
  them into one track record.

## Automated NBA intake (Doc's Sports daily show)

- `nba-channel-intake` (`apps/nfl-audio-parser/nfl_audio_parser/nba_channel.py`,
  run by a timer as the agent account) downloads each new free show,
  transcribes it, extracts picks with a Claude call and ingests them into
  the forwarder opinion store (`/home/forwarder/data/nfl_audio_parser.sqlite3`,
  view `current_opinion_observations`). It then exports the latest pregame
  full-game sides and totals per picker to
  `/var/lib/decision-helper-bot/picker-picks.json`, which Bell (the decision
  helper bot, formerly called Mel) reads. Per-video status is in
  `nba-channel-state.json` beside it.
- The store keeps only 1–2 evidence quotes per pick, and the AssemblyAI
  transcript is deleted after ingest. For fuller quotes, re-download through
  the proxy and transcribe locally with faster-whisper (`small.en`, models in
  `/var/lib/decision-helper-bot/models`): decode to 16 kHz mono wav with
  ffmpeg and pass the samples as an array, because the installed PyAV
  rejects the `metadata_errors` argument when given a file path.
- **Check a show was fully parsed** by comparing the revision's
  `source_end_seconds` with the duration in the channel listing. A
  truncated download once dropped seven of a show's eleven picks; the
  intake now rejects audio under 95% of the listed length.
- **Diagnose a short pick list before guessing:** compare transcript length
  with the video first. A missing-picks show was blamed on speaker mapping
  until the stored range turned out to be the last 177 of 502 seconds. On
  the full audio, AssemblyAI's speaker labels settled an attribution that
  guessing from the transcript's flow got wrong.
- **Reprocess one show:** back up `nba-channel-state.json` and remove the
  show's entry (find it by `slate_date`), then run
  `nba-channel-intake run --since D --until D --dry-run` (then without
  `--dry-run`) with both `~/.claude/auth.env` and
  `~/.config/nfl-audio-parser/env` sourced. Without `auth.env` the Claude
  step fails with "OAuth session expired". Then run
  `nba-channel-intake export`.
- **Store revisions:** a new revision supersedes an old one only with the
  same content, prediction window and source range. Retire a stale range
  by ingesting an empty revision (same URL, publish time, window and range;
  `events` and `observations` empty) with
  `nfl-audio-parser-store opinions-ingest --source-url <url> --input -` run
  as `forwarder`, piping the JSON on stdin: `forwarder` cannot read files in
  the agent's private temp directories.
- The export keeps **one side per game per picker**: when a host gave an
  official spread plus a moneyline "sprinkle", the export kept the
  moneyline lean and dropped the spread.
- First-half picks are stored with `period = 1h` and are not exported.
- Conditional picks carry `condition_text` (a player playing, or how a
  game the night before ends). Resolve them before grading: player
  participation from the box score (`didNotPlay`), prior results from the
  scoreboard. Unresolved conditions should void the pick, not count it.
- Transcripts render "IC" (Indian Cowboy) as "I see".

## Grading gotchas

- Key graded picks by video, picker, selection, market **and line**. Without
  the line, two overs in one video collided and one inherited the other's
  result.
- When the spoken line is missing, fall back to the closing line and mark it.
- Props need player box scores; grade them separately from sides and totals.

*Connections:* sports-data-sources.md (scores and lines for grading),
capper-backtesting.md (what to do with the graded picks), vps-operations.md,
picker_craig.md, picker_indiancowboy.md, nba-picker-tracking.md,
bell-bait-tracker.md (shows the export next to the user's bait).
