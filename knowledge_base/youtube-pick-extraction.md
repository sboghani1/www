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

## Grading gotchas

- Key graded picks by video, picker, selection, market **and line**. Without
  the line, two overs in one video collided and one inherited the other's
  result.
- When the spoken line is missing, fall back to the closing line and mark it.
- Props need player box scores; grade them separately from sides and totals.

*Connections:* sports-data-sources.md (scores and lines for grading),
capper-backtesting.md (what to do with the graded picks), vps-operations.md,
picker_craig.md, picker_indiancowboy.md.
