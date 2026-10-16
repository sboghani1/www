---
name: nfl-audio-parser
description: Transcribe a bounded local audio segment, load the authoritative NFL schedule, and extract reviewable NFL picks with exact speaker quotes, timestamps, and conviction evidence.
---

# NFL audio parser

Use this skill when the user supplies a local audio file, start and end
timestamps, and an NFL season/week and asks to identify the picks stated in
that segment.

This skill works in both Claude Code and GitHub Copilot. Perform the extraction
in the current agent session; do not launch another agent unless the user
explicitly requests delegation.

## Fresh-session request

The intended copyable request is:

```text
Use the nfl-audio-parser skill to extract NFL picks from this audio segment.

Audio path: <AUDIO_PATH>
Start timestamp: <START_TIMESTAMP>
End timestamp: <END_TIMESTAMP>
Season: <NFL_SEASON>
Week: <NFL_WEEK>

Transcribe only the specified segment, load the authoritative NFL schedule for
that week, identify every affirmative straight bet, parlay leg, teaser leg,
team total, and player or team prop, preserve the speaker's exact words and
original-file timestamps, and assess conviction only from explicit language.
Preserve which legs belong to the same parlay or teaser. Produce the review
artifact defined by the skill. Do not persist any picks without my approval.

Example for the Ringer Week 3 recording:

Use the nfl-audio-parser skill to extract NFL picks from this audio segment.

Audio path: /Users/boghani/Downloads/NFL Week 3 Mega-Preview! Ringer Wise Guys.mp3
Start timestamp: 10:47
End timestamp: 1:40:25
Season: 2026
Week: 3

Transcribe only the specified segment, load the authoritative NFL schedule for
that week, identify every affirmative straight bet, parlay leg, teaser leg,
team total, and player or team prop, preserve the speaker's exact words and
original-file timestamps, and assess conviction only from explicit language.
Preserve which legs belong to the same parlay or teaser. Produce the review
artifact defined by the skill. Do not persist any picks without my approval.
```

Accepted timestamps are integer or decimal seconds, `MM:SS[.sss]`, or
`HH:MM:SS[.sss]`.

## Required inputs

Require all five values before running:

- exactly one source:
  - `audio_path`: an existing local file; or
  - `youtube_url`: one public YouTube video;
- `start_timestamp`;
- `end_timestamp`, strictly after the start;
- `season`;
- `week`.

Optional user context may include the program name, episode date, known speaker
names, or which speaker matters. Treat those as hints, not ground truth.

## Invariants

- Never alter the source audio.
- Upload only the requested segment to a transcription provider.
- Never print API keys, Google credentials, environment files, or Telegram
  sessions.
- Treat the authoritative `nfl_games` schedule as context, not permission to
  invent a pick.
- Preserve the exact transcript wording behind every extracted selection.
- Do not silently turn analysis, a recap, a hypothetical, or somebody else's
  quoted opinion into the speaker's pick.
- Do not fill in a line or price from the schedule or current market when the
  speaker did not state it.
- Never copy the original audio to the VPS or store audio bytes, the original
  local path, or the original filename in SQLite.
- Do not append to `celebrity_picks`, create celebrities, or write any Sheet or
  production database. The output is review-only until the user explicitly
  approves a separate import operation.
- Surface uncertainty. An unresolved speaker or event is a review flag, not an
  excuse to guess.

## 1. Create an ephemeral local work directory

Create a private temporary directory on the machine that can read the source
audio. The directory is for the bounded clip and in-progress API responses
only; it is not the durable record.

Do not copy the original audio into the work directory. Read it in place and
create only the requested bounded clip.

Derive an opaque `segment_sha256` from the generated clip bytes. This supports
deduplication without retaining the source path or filename.

Temporary files may include:

```text
segment.<audio-extension>
transcript.raw.json
transcript.json
schedule.json
review.json
```

Never treat a partial provider response as completed. After the parsed
transcript and review data are committed to the VPS database, remove the exact
temporary directory and all generated files inside it. Never remove or modify
the original audio.

## 2. Validate and clip the audio

Use the checked-in transcription CLI, which performs timestamp parsing,
`ffprobe` validation, bounded `ffmpeg` clipping, SHA-256 calculation, AssemblyAI
upload/polling, timestamp normalization, and temporary-audio cleanup:

```bash
PYTHONPATH=packages/www-sqlite:apps/nfl-audio-parser \
  python3 -m nfl_audio_parser.transcribe \
  --audio "<AUDIO_PATH>" \
  --start "<START_TIMESTAMP>" \
  --end "<END_TIMESTAMP>" \
  --output "<temporary-work-directory>/transcript.json"
```

For a YouTube source, replace `--audio` with:

```bash
  --youtube-url "<YOUTUBE_URL>"
```

The command downloads the best available audio with `yt-dlp` into its private
temporary directory and removes it after the bounded segment is transcribed.
Only the requested segment may be uploaded to AssemblyAI. Do not persist the
URL, video title, downloaded filename, or downloaded source path.

When installed, use `nfl-audio-parser-transcribe` instead of `python3 -m`.

Keep both coordinate systems:

- `segment_seconds`: starts at zero in the clipped file;
- `source_seconds`: `segment_seconds + requested_start_seconds`.

All human-facing timestamps in `review.json` must use `source_seconds`, so the
user can jump to the quoted language in the original file.

Fail clearly if `ffmpeg` or `ffprobe` is unavailable. Do not upload the full
recording as a fallback.

## 3. Transcribe with speaker and word timestamps

The transcription CLI uses AssemblyAI when `ASSEMBLYAI_API_KEY` is available.
On a local workstation,
load it from the process environment first, then from
`~/.config/nfl-audio-parser/env` when the environment variable is unset. The
config directory must be mode `0700` and the file mode `0600`. Parse only the
`ASSEMBLYAI_API_KEY` assignment; never source arbitrary shell content from the
file.

It:

1. uploads the clipped file through `POST /v2/upload`;
2. submits `POST /v2/transcript` with:
   - `speaker_labels: true`;
   - `punctuate: true`;
   - `format_text: true`.
3. may add supported keyterm prompting for NFL team names and common betting terms
   only when the currently installed/API-supported AssemblyAI model accepts it.
   If the provider rejects an optional vocabulary field, remove only that field
   and resubmit once. Do not disable diarization.
4. polls `GET /v2/transcript/<id>` until `completed` or `error`;
5. emits only the normalized `transcript.json`.

The API key belongs in the environment and must never be written into an
artifact or command output.

Normalize `transcript.json` to:

```json
{
  "provider": "assemblyai",
  "provider_transcript_id": "<id>",
  "source_start_seconds": 0.0,
  "source_end_seconds": 0.0,
  "audio_duration_seconds": 0.0,
  "language": "en",
  "utterances": [
    {
      "utterance_index": 0,
      "speaker_label": "A",
      "segment_start_seconds": 0.0,
      "segment_end_seconds": 0.0,
      "source_start_seconds": 0.0,
      "source_end_seconds": 0.0,
      "confidence": 0.0,
      "text": "<exact provider utterance>",
      "words": [
        {
          "text": "<exact provider token>",
          "segment_start_seconds": 0.0,
          "segment_end_seconds": 0.0,
          "source_start_seconds": 0.0,
          "source_end_seconds": 0.0,
          "confidence": 0.0
        }
      ]
    }
  ]
}
```

If AssemblyAI credentials are unavailable, stop and report that
`ASSEMBLYAI_API_KEY` is required. Do not silently switch providers or use a
non-diarized transcript unless the user approves that change.

After the normalized transcript and extraction have been durably committed to
the VPS database, request deletion of the provider transcript when the
provider supports deletion. Report a deletion failure, but never delete the
durable parsed transcript because provider cleanup failed.

## 4. Load authoritative schedule context

Load `nfl_games` from the `telegram-channel-forwarder` environment. Include the
requested week and, when present, the immediately preceding and following
weeks. Adjacent weeks help distinguish recaps and look-ahead picks.

On the receptionist VPS, run read-only schedule loading in the trusted
production account:

```bash
sudo -n -u forwarder -H bash -lc \
  'cd /home/forwarder/app && /home/forwarder/venv/bin/python <temporary-read-only-schedule-script>'
```

The temporary script must:

1. load `/home/forwarder/app/.env` and `.env.local` using the repository's
   existing dotenv order;
2. import `GAME_HEADERS` and `get_gspread_client` from `nfl_lines`;
3. open `NFL_INTAKE_SHEET_ID`;
4. read `nfl_games` with `expected_headers=GAME_HEADERS`;
5. emit only rows for the requested season and weeks `week - 1`, `week`, and
   `week + 1`;
6. perform no writes.

Outside the VPS, use the sibling `telegram-channel-forwarder` checkout and the
same read-only imports when its credentials are available. If authoritative
schedule access is unavailable, stop rather than obtaining a schedule from the
web or reconstructing one from memory.

Store a compact `schedule.json` containing:

```json
{
  "season": 2026,
  "requested_week": 2,
  "games": [
    {
      "event_id": "<id>",
      "season": 2026,
      "week": 2,
      "status": "upcoming",
      "away_team": "<team>",
      "home_team": "<team>",
      "commence_time_utc": "<timestamp>",
      "commence_time_et": "<timestamp>"
    }
  ]
}
```

Do not include market lines in the extraction context by default. Market data
can anchor the agent to a number the speaker never stated.

## 5. Identify speakers

Map provider labels to real people only when the transcript or user-supplied
context supports the mapping. Evidence can include:

- an introduction;
- another speaker addressing the person by name;
- self-identification;
- user-provided known-speaker context.

For every mapping, record:

```json
{
  "speaker_label": "A",
  "name": "<name or null>",
  "confidence": 0.0,
  "evidence": [
    {
      "utterance_index": 0,
      "exact_text": "<exact transcript text>"
    }
  ]
}
```

Never create a celebrity identity from voice recognition or unsupported
familiarity. Leave `name` null when uncertain.

## 6. Extract pick statements

Read the full bounded transcript in chronological order. Identify explicit
statements involving the requested week's NFL games.

An affirmative pick includes:

- a direct selection;
- a stated lean, when clearly presented as the speaker's preference;
- one leg of a parlay or teaser;
- a player or team prop;
- a conditional pick, provided the condition is preserved.

Do not classify these as active picks:

- reporting another person's selection;
- discussing consensus or market movement without choosing a side;
- recapping a result;
- explaining what would have happened;
- an abandoned hypothetical;
- sarcasm or a joke;
- an explicit pass;
- a pick the same speaker clearly retracts later, unless it remains in the
  statement history with `final_status: "retracted"`.

Normalize each selection to:

- `bet_structure`: `straight`, `parlay`, or `teaser`;
- `group_id`: a stable value shared by all legs of the same parlay or teaser,
  otherwise null;
- `leg_index`: one-based order within a parlay or teaser, otherwise null;
- `teaser_points`: the stated adjustment for a teaser, otherwise null;
- `bet_type`: `spread`, `moneyline`, `total`, `team_total`, `prop`, or
  `other`;
- `period`: `game`, `1h`, `2h`, `1q`, `2q`, `3q`, or `4q`;
- exact team names from `schedule.json`;
- `direction`;
- `line` only when stated;
- `price` only when stated;
- `player` and `prop_stat` when applicable.

Resolve an `event_id` only when the transcript and schedule identify exactly
one game. Keep `event_id: null` and add a review flag when resolution is
ambiguous.

## 7. Preserve the speaker's exact words

Every pick must contain one or more `evidence_segments`. Each segment must copy
text exactly from one transcript utterance:

```json
{
  "utterance_index": 12,
  "speaker_label": "A",
  "source_start_seconds": 418.2,
  "source_end_seconds": 423.8,
  "exact_text": "I absolutely love Green Bay laying three and a half here."
}
```

Before writing `review.json`, deterministically verify that every
`exact_text` is an exact substring of the referenced utterance and that its
timestamps fall inside that utterance. Never present a model paraphrase as a
quote.

Keep quotations focused. Include the selection sentence and short adjacent
rationale, but never copy the entire transcript into one pick.

When the speaker changes a pick, preserve each chronological statement and
mark the final disposition. Do not discard the earlier quote.

## 8. Score conviction from explicit language

Conviction measures what the speaker said, not volume, cadence, or the agent's
agreement with the analysis.

Use this scale:

| Score | Label | Typical explicit evidence |
|---:|---|---|
| 5 | best_bet | "best bet", "bet of the week", "my lock", "hammer it" |
| 4 | strong | "love", "all over", "definitely", "I'm betting" |
| 3 | standard | unhedged direct selection without a strength modifier |
| 2 | lean | "lean", "slight edge", "I prefer", "small play" |
| 1 | speculative | "maybe", "could see", "if I had to", highly conditional |
| 0 | no_pick | pass, analysis only, hypothetical, quoted other person |

Record:

```json
{
  "score": 4,
  "label": "strong",
  "explicit_markers": ["absolutely love"],
  "hedged": false,
  "conditional": false,
  "condition_text": null
}
```

`explicit_markers` must be exact words found in the evidence segments. If the
language is mixed, choose the lower supported score and add a review note.

## 9. Produce the parsed review payload

Use this top-level contract:

```json
{
  "schema_version": "nfl_audio_picks_v1",
  "request": {
    "start_timestamp": "<user value>",
    "end_timestamp": "<user value>",
    "start_seconds": 0.0,
    "end_seconds": 0.0,
    "season": 2026,
    "week": 2,
    "segment_sha256": "<sha256 of bounded clip>"
  },
  "transcription": {
    "provider": "assemblyai",
    "provider_transcript_id": "<id>",
    "utterance_count": 0,
    "speaker_count": 0
  },
  "speakers": [],
  "picks": [
    {
      "pick_index": 1,
      "speaker_label": "A",
      "speaker_name": null,
      "event_id": "<id or null>",
      "away_team": "<team or null>",
      "home_team": "<team or null>",
      "bet_structure": "straight",
      "group_id": null,
      "leg_index": null,
      "teaser_points": null,
      "bet_type": "spread",
      "period": "game",
      "selection": "<canonical selection>",
      "direction": "<team, Over, or Under>",
      "line": -3.5,
      "price": -110,
      "player": null,
      "prop_stat": null,
      "final_status": "active",
      "conviction": {},
      "evidence_segments": [],
      "rationale_segments": [],
      "transcription_confidence": 0.0,
      "extraction_confidence": 0.0,
      "review_flags": []
    }
  ],
  "non_pick_mentions": [
    {
      "speaker_label": "A",
      "reason": "quoted_other_person",
      "evidence_segments": []
    }
  ],
  "review_summary": {
    "active_pick_count": 0,
    "retracted_pick_count": 0,
    "unresolved_speaker_count": 0,
    "unresolved_event_count": 0,
    "warnings": []
  }
}
```

Use `null`, not an invented value, for unstated lines, prices, people, or
events.

The payload must not contain the source path, source filename, audio bytes, API
credentials, or environment values.

## 10. Persist parsed data to VPS SQLite

The only durable store is:

```text
/home/forwarder/data/nfl_audio_parser.sqlite3
```

The database is owned by `forwarder` and exists only on the VPS. From the
receptionist, execute database operations as:

```bash
sudo -n -u forwarder -H \
  /opt/telegram-receptionist/current/.venv/bin/nfl-audio-parser-store ingest \
  --input <temporary-ingest-payload.json>
```

From a local Claude or Copilot session, transmit only the normalized
`transcript.json`, compact `schedule.json`, and parsed review payload over the
existing SSH connection to the VPS. Never transmit the source audio, generated
clip, source path, or raw provider response.

Wrap those three objects in this ingest envelope:

```json
{
  "schema_version": "nfl_audio_parser_ingest_v1",
  "transcript": {},
  "schedule": {},
  "review": {}
}
```

The checked-in `nfl-audio-parser-store` CLI owns schema creation, migrations,
transactions, idempotency, exact-quote validation, and append-only extraction
revisions. Do not create or alter its tables from the skill.

For a local session, stream the payload without creating a durable remote
source file:

```bash
cat <temporary-ingest-payload.json> |
  ssh root@209.38.51.86 \
    'runuser -u forwarder -- /opt/telegram-receptionist/current/.venv/bin/nfl-audio-parser-store ingest'
```

The CLI returns `run_id`, `revision_id`, `created_run`, and
`created_revision`. Capture that receipt before local cleanup.

The store keeps normalized utterances, exact quotes, conviction evidence,
schedule context, parsed picks, and review decisions. It rejects:

- original or clipped audio;
- source path or filename;
- raw provider response;
- API credentials;
- unrelated episode metadata.

The same `(segment_sha256, season, week)` and transcript is idempotent. A
changed extraction appends a revision. A conflicting transcript for the same
segment is rejected rather than silently replacing the transcript.

SQLite is a staging and audit store. It is not the production
`celebrity_picks` store and must never be confused with the MOE SQLite
database.

## 11. Report to the user

Present a compact review grouped by speaker:

```text
Speaker/name — Team -3.5 — strong (4/5)
00:06:58.200-00:07:03.800: "I absolutely love..."
Reason: explicit marker "absolutely love"
Event: Away @ Home (<event_id>)
Flags: none
```

Also state:

- the VPS database path and `run_id`;
- transcription provider and ID;
- requested source range;
- schedule season/week;
- unresolved speakers/events;
- retractions, conditionals, or low-confidence transcript passages.

Stop after review output. Importing approved picks is a separate operation that
must use the forwarder's existing `get_or_create_celebrity`,
`build_freeform_celebrity_submissions`, `build_celebrity_rows`, and
`append_celebrity_picks` paths rather than writing rows directly.

## Cleanup

After the VPS transaction succeeds, remove the exact temporary work directory,
including the generated clip, raw provider response, normalized JSON files, and
review payload. The normalized transcript and parsed information in VPS SQLite
are the durable record. Never delete or modify the original audio.
