# Mel — decision helper bot

Private Telegram bot `@decision_helpers_bot` (display name **Mel**). Send it
voice notes about your NFL and NBA picks. For each voice note Mel asks, with
buttons, whether to **Parse** it or **Trash** the audio. Parsed notes produce
committed picks and tentative leans (tracked separately). Each game has one
decision per market family (side = moneyline/spread, and total). Sides you
moved off are kept as that decision's path, so flips can be analyzed. Mel only
confirms what it recorded; it doesn't send analysis on input notes. It follows
the BetOnline line until kickoff or tip-off, grades results from ESPN, and
reviews each decision quietly after the game. `/games` lists today's and
tomorrow's games, and tapping one gets Mel's opinion. Picks for games that already started in the last three days can be
logged late; they are marked `[logged after start]`, use the line you state
(or BetOnline's closing line, marked `[BetOnline close]`), and are graded on
the next sync.

## How it works

1. **Audio → text on the VPS.** Claude cannot take audio input, so Mel uses
   local [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   (`small.en`, CPU int8). It's free and audio never leaves the server. Voice notes,
   audio uploads (m4a/mp3/ogg), and audio documents are accepted, up to 20 MB.
   Plain text messages are treated as a transcript. The audio of every parsed
   note is kept in `/var/lib/decision-helper-bot/audio/<entry-id>.<ext>` and
   its exact transcript in `entries.transcript`, so notes can be reparsed (or
   re-transcribed) after a prompt or model change. Only **Trash** and
   **Remove** delete them.
2. **Text → picks with Claude.** The transcript and the candidate games (ESPN
   scoreboards from three days back through the next eight days, with
   BetOnline lines attached) are sent to Claude Code (`claude -p`, default
   `claude-opus-5-5`, medium effort). Claude returns strict JSON. Mel rejects
   any game or team that is not in the candidate list. Each game and family
   gets one `final` row, the position you ended on. Abandoned sides are
   stored with `role='superseded'`, and so are views replaced by a later
   note. Weighing the moneyline against the spread on the same team is one
   decision, with the alternative noted. You can send several notes per
   game. Each candidate game carries your prior views on it (earlier sides
   and notes), so "still on them" resolves to the right side. A note that
   discusses a game without taking a side is stored in `game_notes` as
   `context`. Explicitly passing on a side or total ("I'm going to pass")
   is stored in `passes`; it cancels your earlier views on that market (all
   become superseded, so the game has no position) until a later note takes
   a side again. A side or note sent after the game is graded becomes a
   `hindsight` note and is never counted as a decision. Reparsing a note
   clears its picks (graded ones too), passes, and attachments, then parses the saved
   transcript again with the note's original time, BetOnline lines, and only
   the views sent before it; new picks for finished games are re-graded from
   ESPN on the next sync.

   Two per-note corrections in `entries` survive reparses: `pregame=1` means
   the note holds views from before tip even though it was sent later (no
   `[logged after start]` mark), and `clarification` is extra speaker text
   (for example "I bet the Seahawks spread") that the parser trusts over the
   transcript. Views logged at or after tip have no closing-line value, since
   their line is the close.

   A side is saved as a spread unless you explicitly take the moneyline ("Jazz
   to win", "moneyline"/"ML"). The line is only a number you say you are
   taking; otherwise BetOnline's line is used. A number you project ("should
   be like minus eight to nine and a half", "assuming they're +3.5") is saved
   as your number (`my_line_low`/`my_line_high`, in the selected team's
   spread or the projected total), shown as `[your number -9.5 to -8]`, and
   compared with BetOnline's line in reviews and `/games` opinions.
3. **Market tracking (BetOnline only, no API credits).** Mel never calls The
   Odds API. It reads BetOnline lines from the `nfl_games` / `nba_games` tabs
   of telegram-channel-forwarder's intake workbook (written by its
   `nfl-lines-fetcher` / `nba-lines-fetcher` timers), using the forwarder's
   Google service account. The line and price are saved when the pick is made,
   along with the BetOnline spread at that moment (the picked team's for a
   moneyline or spread, the favorite's for a total), shown as
   `[BetOnline spread Bulls +3.5]` and given to reviews and `/games` opinions.
   Lines are then re-read at T-24h, 6h, 1h, and 15 minutes after tip. The forwarder's
   last poll lands in a game's final 12 minutes, so that post-tip read is
   BetOnline's closing line. Picks are timestamped when the note was sent.
   A note parsed more than 10 minutes later (for example via `/reparse`) gets
   the line from the `*_line_snapshots` tab at that time, then the current line
   on the next sync. A spread or total noted before BetOnline posts takes the
   first line read after it posts.
4. **Results.** ESPN final scores grade each pick and compute closing-line
   value (points for spreads and totals, implied probability for moneylines).
   Superseded sides are graded too, as if kept, but only final positions
   count toward the record.
5. **Post-game review (silent).** One review per decision. Claude scores the
   process (1-5) using the full transcript of every note you sent about the
   game, the context notes, when you said them relative to the game, the path
   (e.g. `Pistons to win (WIN) → Celtics to win (LOSS)`), the result, and the
   market move, and flags biases. Reviews are stored, not sent. They feed
   `/stats` and the lessons used in `/games` opinions. `/stats` adds a "Changing sides" section: flips that
   helped or hurt, and first view vs. final position. There is no weekly report.
   Each review also sees the rules from the last three daily learnings.

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

6. **Opinions (`/games`).** Lists all of today's and tomorrow's (ET) NFL and
   NBA games with the BetOnline spread and total. Tapping a game sends Claude
   the current and opening lines, everything you said about the game, your
   record, flip history, bias flags, and recent review lessons. Mel replies
   with a critique of your view and its own call (play with the worst line
   worth taking, or pass), a confidence, and what would change it. Claude
   has no news or injury feed. It also sees the last three days of learnings
   (summary, what didn't work, rules to try).
7. **Daily learnings (silent).** At 9:00 ET each morning Mel studies the
   previous ET day: every game you talked about (picks, leans, passes, and
   context notes), the full transcripts, the BetOnline line and spread you
   got, your own number, the closing line, the final score with each side's
   actual margin (or total points), and the post-game reviews. Claude writes a
   summary, what worked and didn't, your number vs. BetOnline's, flips,
   calibration, 1-3 rules to try, and whether earlier rules were followed.
   A day waits until all its picks are graded and reviewed, or 9 ET the
   following day at the latest. One day runs per sync tick, oldest first, so
   past days backfill automatically. Learnings are stored (not sent) and shown
   with `/learnings`.

## Bot commands

- `/games` - today's and tomorrow's games; tap one for Mel's opinion
- `/recent` - the 10 newest picks and leans with status
- `/stats` - committed picks and leans: record, Brier calibration, process
  score, CLV, and top bias flags
- `/learnings [YYYY-MM-DD]` - the latest (or that ET day's) daily learnings
- `/reparse` - re-run parsing on the latest saved transcript, or reply
  `/reparse` to any of your voice/text notes to reparse that one (graded
  notes included; keeps the note's original time and line)
- `/void <id-prefix>` - drop a mis-parsed open pick
- **🔁 Reparse** (button on each saved note) - same as replying `/reparse`
- **🗑 Remove this note** (button on each saved note) - deletes that note's
  picks/leans (even graded ones), attached game notes, transcript, and audio
  so you can resend what you meant; an earlier view of the game becomes final
  again
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
