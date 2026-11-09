# NFL/NBA audio opinion store

This app has two append-only layers:

1. An evidence store for normalized transcripts, exact pick quotes, conviction
   evidence, schedule context, and extraction revisions.
2. A provider-neutral NFL/NBA opinion store for tracking how a picker's view
   changes across content, grading results, measuring relative conviction, and
   analyzing agreement between pickers.

It never persists source URLs, titles, audio paths, filenames, audio bytes, or
raw provider responses. A supplied YouTube URL is canonicalized to its video
identifier and irreversibly hashed. Supplying the same URL later recreates the
hash, so the content remains queryable by URL without storing the URL itself.

The default database is:

```text
/home/forwarder/data/nfl_audio_parser.sqlite3
```

Initialize and ingest as the `forwarder` account:

```bash
nfl-audio-parser-store init
cat parsed-payload.json | nfl-audio-parser-store ingest
```

Clip and transcribe a local source without retaining the generated audio:

```bash
nfl-audio-parser-transcribe \
  --audio "/path/to/episode.mp3" \
  --start 10:47 \
  --end 1:40:25 \
  --output /tmp/transcript.json
```

The same command accepts one public YouTube video instead of a local file:

```bash
nfl-audio-parser-transcribe \
  --youtube-url "https://www.youtube.com/watch?v=uZIO0qZslXE" \
  --start 1:04 \
  --end 9:54 \
  --sport nfl \
  --games-from 2026-10-18 \
  --games-through 2026-10-19 \
  --output /tmp/transcript.json
```

`--audio` and `--youtube-url` are mutually exclusive. YouTube audio is
downloaded with `yt-dlp` into a private temporary directory, clipped with the
same FFmpeg path as local audio, and removed after transcription. Only the
requested clip is uploaded to AssemblyAI. The normalized transcript does not
contain the URL, video title, source filename, or downloaded media path.

The command reads `ASSEMBLYAI_API_KEY` from the environment or the private
mode-0600 file `~/.config/nfl-audio-parser/env`. The output contains no source
path or filename. When `--sport`, `--games-from`, and `--games-through` are
provided, all three are required. The normalized transcript records that
NFL/NBA prediction window plus a hashed source reference, never the raw URL.

The ingest envelope uses `schema_version=nfl_audio_parser_ingest_v1` and
contains normalized `transcript`, `schedule`, and `review` objects. Repeating
the same content is idempotent; a changed extraction for the same transcript
creates an append-only extraction revision.

The reusable connection, transaction, and migration primitives live in
`packages/www-sqlite`.

## Opinion ingestion

Opinion analytics use `schema_version=sports_opinions_v1`. The source URL is a
CLI argument rather than a JSON field:

```bash
nfl-audio-parser-store opinions-ingest \
  --source-url "https://www.youtube.com/watch?v=..." \
  --input opinions.json
```

The payload declares the sport and the dates for which the discussed games are
valid:

```json
{
  "schema_version": "sports_opinions_v1",
  "content": {
    "source_provider": "youtube",
    "program_name": "Sharp or Square",
    "published_at_utc": "2026-10-15T16:00:00Z",
    "sport": "nfl",
    "valid_from_date": "2026-10-18",
    "valid_through_date": "2026-10-19",
    "valid_timezone": "America/New_York",
    "source_start_seconds": 797,
    "source_end_seconds": 3086,
    "segment_sha256": "<bounded segment hash>"
  },
  "events": [
    {
      "event_id": "<authoritative event id>",
      "sport": "nfl",
      "commence_time_utc": "2026-10-19T20:25:00Z",
      "away_team": "Kansas City Chiefs",
      "home_team": "Las Vegas Raiders",
      "status": "scheduled"
    }
  ],
  "observations": [
    {
      "observation_index": 1,
      "picker_name": "Simon Hunter",
      "event_id": "<authoritative event id>",
      "market_family": "game_side",
      "contract_type": "spread",
      "period": "game",
      "source_seconds": 900,
      "selection": "Las Vegas Raiders +4.5",
      "direction": "Las Vegas Raiders",
      "line": 4.5,
      "opinion_status": "active",
      "opinion_stage": "early_look",
      "conviction": {
        "score": 4,
        "label": "strong",
        "explicit_markers": ["love"],
        "hedged": false,
        "conditional": false,
        "condition_text": null
      },
      "evidence_segments": [
        {"exact_text": "I love Las Vegas plus four and a half."}
      ],
      "extraction_confidence": 0.97,
      "review_flags": []
    }
  ]
}
```

Supported sports are `nfl` and `nba`. Prediction-window validation uses the
declared timezone, so Sunday-night NFL games represented by a next-day UTC
timestamp still match their local game date.

The same video may receive append-only corrected extraction revisions. Queries
use only the latest revision for each video segment and prediction window.

## Opinion semantics

Every statement is an observation in a stable picker/event/market thread.
Repeated opinions are retained rather than overwritten. The timeline view
classifies each observation as:

- `initial`
- `reaffirmed`
- `strengthened`
- `weakened`
- `changed_line`
- `changed_contract`
- `switched_direction`
- `passed`
- `retracted`

Absolute conviction remains the evidence-backed 0–5 scale. SQL views also
calculate the observation's rank within the picker's prediction window and
within the video. Spread observations automatically receive a
`favorite`/`underdog`/`pickem` role from the stated line. Moneyline observations
can provide `side_role` explicitly when known.

## Querying

Show every current pick and represented game for a video:

```bash
nfl-audio-parser-store source \
  --source-url "https://www.youtube.com/watch?v=..."
```

Show how one picker changed their opinion:

```bash
nfl-audio-parser-store timeline \
  --picker "Simon Hunter" \
  --event-id "<event id>" \
  --market-family game_side
```

Group latest pregame opinions by direction:

```bash
nfl-audio-parser-store consensus \
  --event-id "<event id>" \
  --market-family game_side \
  --minimum-conviction 3
```

## Grading and records

Record a final score and grade every current full-game spread, moneyline, and
total observation at its stated contract:

```bash
nfl-audio-parser-store result \
  --event-id "<event id>" \
  --away-score 27 \
  --home-score 20
```

Props and non-full-game markets remain explicitly `ungradeable` until an
appropriate result feed exists; the store does not invent period or player
results.

Records default to one latest pregame opinion per picker/event/market thread,
so repeated confirmations do not inflate results:

```bash
nfl-audio-parser-store stats \
  --group-by picker \
  --group-by conviction_tier \
  --group-by side_role \
  --group-by agreement_bucket \
  --sport nfl
```

Available dimensions are `picker`, `sport`, `conviction_tier`, `side_role`,
`market_family`, `change_type`, `opinion_stage`, `program`, and
`agreement_bucket`, plus exact `agreement_count` and `picker_count` dimensions.
Agreement buckets are `solo`, `unanimous`, `majority`, `split`, and `minority`.
Use `--policy all_observations` for research on every stated opinion instead
of final pregame records.

Spotify content identifiers can already be fingerprinted by the opinion store,
but Spotify downloading/transcription is intentionally not implemented yet.

## Daily NBA channel intake

`nba-channel-intake` (`nfl_audio_parser/nba_channel.py`) ingests daily NBA
picks shows into the opinion store. It is installed into Mel's environment
and scheduled by Mel's installer as `nba-channel-intake.timer` (see
`apps/decision-helper-bot`), running as `receptionist-agent`. Its only
channel today is Doc's Sports with Craig Trapp and Indian Cowboy (`CHANNELS`).

Each run:

1. Lists the channel's recent uploads (flat listing, no download) and keeps
   public titles that mention NBA and a configured picker, skipping
   members-only/"Premium", WNBA, preseason, and other sports. The slate date
   comes from the title (`11/9/26` or `November 9th, 2026`).
2. Downloads the audio through `NBA_CHANNEL_PROXY`: YouTube answers "confirm
   you're not a bot" to downloads from the VPS address, so the installer
   copies the forwarder's local Cloudflare WARP proxy (`PINNACLE_PROXY`) into
   `~/.config/nfl-audio-parser/env`.
3. Transcribes the whole video with AssemblyAI (speaker labels on) and deletes
   the AssemblyAI transcript afterwards.
4. Loads ESPN's NBA games for the title date, the publish date, and the day
   after, keeping only games that tip after the video went up (one show was
   titled with an old date).
5. Asks Claude (text only, subscription login, `claude-opus-5-5`) for each
   host's full-game and period sides and game totals with exact quotes, then
   keeps only picks whose quotes are exact transcript substrings for a listed
   game and an expected host, validates the payload in a throwaway store, and
   ingests it as the forwarder account. The prediction window is the picked
   games' dates.
6. Exports the pickers' latest pregame picks and covered dates
   (`nfl-audio-parser-store picks-export`) to
   `/var/lib/decision-helper-bot/picker-picks.json`.

Run state lives in `/var/lib/decision-helper-bot/nba-channel-state.json`,
keyed by source reference hash only. Ingested and no-pick videos are not
retried; failures are retried up to three times. By default a run covers
slates from yesterday through tomorrow; backfill with
`nba-channel-intake run --since 2026-10-20 --until 2026-11-08 --scan 1100
--max-items 20`, and `--dry-run` prints the payload without ingesting.

## Scheduled channel pipeline

`nfl-audio-parser-pipeline` discovers and processes configured YouTube channels
without storing video URLs, identifiers, titles, or descriptions in its
operational database. The checked-in `nfl_audio_parser/channels.json` registry
defines each channel locator, classifier rules, season, expected speakers, and
opaque hashes for already reviewed or duplicate videos. Add another channel by
adding one registry object; code changes are not required for another YouTube
channel with the same extraction workflow.

The separate lifecycle database defaults to:

```text
/var/lib/nfl-audio-parser/pipeline.sqlite3
```

It records opaque source hashes, classification and processing statuses,
attempts, leases, retry times, transcript fingerprints, duplicate lineage, and
review/opinion ingest receipts. Raw discovery metadata and extraction job files
exist only for the active process in private temporary directories.

Useful commands:

```bash
nfl-audio-parser-pipeline init
nfl-audio-parser-pipeline discover --channel sharp-or-square
nfl-audio-parser-pipeline run --channel sharp-or-square --max-items 1
nfl-audio-parser-pipeline backfill \
  --channel sharp-or-square --season 2026 --weeks 1-6 --max-items 100
nfl-audio-parser-pipeline status
```

Discovery records non-candidates as `skipped` and queues likely pick videos.
The worker holds a global nonblocking lock and processes queued videos
sequentially. Each candidate is fully transcribed within its known duration,
matched to the authoritative schedule, checked against hashed transcript
shingles for derivative clips, extracted by the headless review agent, schema
validated, and ingested into the review and opinion stores. Opinion evidence is
linked to the canonical extraction revision. AssemblyAI transcript cleanup is
attempted on every terminal path and any cleanup failure is retained in the
pipeline receipt or lifecycle error.

The deployed `nfl-audio-parser-channel.timer` runs daily at 08:00
`America/New_York`. Deployment preserves an existing private
`/home/receptionist-agent/.config/nfl-audio-parser/env`, can migrate the
credential from the forwarder account's private parser config, and refuses to
enable the timer when `ASSEMBLYAI_API_KEY` is unavailable.
