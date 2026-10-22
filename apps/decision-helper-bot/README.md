# Mel — decision helper bot

Private Telegram bot `@decision_helpers_bot` (display name **Mel**). Send it
voice notes about your NFL and NBA picks. For each voice note Mel asks, with
buttons, whether to **Parse** it or **Trash** the audio. Parsed notes produce
committed picks and tentative leans (tracked separately; every side you lean
toward is kept so flips can be analyzed). Mel follows the BetOnline line until
kickoff or tip-off, grades results from ESPN, and reviews each decision after the
game. Picks for games that already started in the last three days can be
logged late; they are marked `[logged after start]`, use the line you state
(or BetOnline's closing line, marked `[BetOnline close]`), and are graded on
the next sync.

## How it works

1. **Audio → text on the VPS.** Claude cannot take audio input, so Mel uses
   local [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   (`small.en`, CPU int8). It's free and audio never leaves the server. Voice notes,
   audio uploads (m4a/mp3/ogg), and audio documents are accepted, up to 20 MB.
   Plain text messages are treated as a transcript.
2. **Text → picks with Claude.** The transcript and the candidate games (ESPN
   scoreboards from three days back through the next eight days, with
   BetOnline lines attached) are sent to Claude Code (`claude -p`, default
   `claude-opus-5-5`, medium effort). Claude returns strict JSON. Mel rejects
   any game or team that is not in the candidate list.
3. **Market tracking (BetOnline only, no API credits).** Mel never calls The
   Odds API. It reads BetOnline lines from the `nfl_games` / `nba_games` tabs
   of telegram-channel-forwarder's intake workbook (written by its
   `nfl-lines-fetcher` / `nba-lines-fetcher` timers), using the forwarder's
   Google service account. The line and price are saved when the pick is made,
   then re-read at T-24h, 6h, 1h, and 15 minutes after tip. The forwarder's
   last poll lands in a game's final 12 minutes, so that post-tip read is
   BetOnline's closing line.
4. **Results.** ESPN final scores grade each pick and compute closing-line
   value (points for spreads and totals, implied probability for moneylines).
5. **Post-game review.** Claude scores the process (1-5) using your original
   words, the result, and the market move, and flags biases. Mel sends this
   immediately after each game. There is no weekly report.

## Claude subscription auth

Mel calls Claude through the same Claude subscription OAuth login as the
receptionist's Opus agent, not with API billing:

- The service runs as `receptionist-agent` with
  `HOME=/home/receptionist-agent`, so it shares `~/.claude/.credentials.json`
  and the optional `~/.claude/auth.env`.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and the
  Bedrock and Vertex switches are removed before every call. Claude Code would
  otherwise prefer them over the subscription.
- Calls are text-only: `--tools ""`, `--strict-mcp-config`,
  `--no-session-persistence`, and no permission bypass.
- If the login expires, Mel replies that Claude needs re-authentication. Run
  `/reauth` on the watchdog bot, the same as for the receptionist. Failed reviews
  retry automatically later.

## Bot commands

- `/recent` - the 10 newest picks and leans with status
- `/stats` - committed picks and leans: record, Brier calibration, process
  score, CLV, and top bias flags
- `/reparse` - re-run parsing on the latest saved transcript (voids its open
  picks first)
- `/void <id-prefix>` - drop a mis-parsed open pick
- `/help`

## Setup

1. In BotFather, create `@decision_helpers_bot`, then `/setname` → `Mel`.
2. Install or update (from the receptionist host):

   ```bash
   systemd-run --unit=decision-helper-bot-install-$(date +%s) --collect --wait --pipe \
     /bin/bash /home/receptionist/repos/www/apps/decision-helper-bot/deploy/install-decision-helper-bot
   ```

   The installer:
   - checks that `claude auth status` shows a subscription login
   - builds a release under `/opt/decision-helper-bot`
   - runs the tests
   - pre-downloads the Whisper model
   - makes one real Claude subscription smoke call before switching releases
3. Configure once, interactively:

   ```bash
   sudo /usr/local/sbin/configure-decision-helper-bot
   ```

   It prompts for the BotFather token without echoing it (re-runs keep the
   existing token). It reuses Receptionist2's allowlisted Telegram user ID and
   copies `GOOGLE_CREDENTIALS` plus the intake sheet ID
   (`DECISION_HELPER_LINES_SHEET_ID`) from `/home/forwarder/app/.env`, then
   enables the service. The installer runs it automatically when an older env
   file lacks the sheet settings.

Optional environment overrides (in
`/home/receptionist-agent/.config/decision-helper-bot/env`):
- `DECISION_HELPER_CLAUDE_MODEL`
- `DECISION_HELPER_CLAUDE_EFFORT`
- `DECISION_HELPER_WHISPER_MODEL`
- `DECISION_HELPER_SYNC_INTERVAL_SECONDS`

## Tests

```bash
cd apps/decision-helper-bot && python -m pytest
```
