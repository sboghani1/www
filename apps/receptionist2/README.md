# Receptionist2

Receptionist2 is a durable Telegram control plane for coding-agent work. It
replaced the original receptionist (`apps/telegram-receptionist`, now retired)
on the same Telegram bot token.

## Services

- `receptionist2-gateway.service`: Telegram intake and outbox delivery
- `receptionist2-worker.service`: durable queued Claude execution, up to
  `RECEPTIONIST2_MAX_PARALLEL_RUNS` (default 2) chats at once
- `receptionist2-watchdog.service`: independent deterministic controls,
  `/reauth`, `/diagnose`, and `/investigate`
- `receptionist2-provider-monitor.timer`: authentication, model-limit, and
  host memory (OOM kill and sustained swap) alerts every 15 minutes
- `receptionist2-maintenance.timer`: daily database backup (14 days), record
  pruning (30 days), and run-artifact cleanup (14 days)

The gateway and worker communicate only through SQLite. Restarting the gateway
does not terminate an active worker run, and completed responses remain in the
outbox until Telegram delivery succeeds.

## Telegram commands

Primary bot:

- `/start`, `/help`, `/status`
- `/new [name]`, `/sessions`, `/switch <id-prefix>`, `/topics`
- `/model [alias-or-id]`, `/effort [level]` (default `claude-opus-5-5`, high)
- `/verbose` toggles live tool progress in the run status message
- `/stop`, `/recover`, `/reset`, and the persistent recovery keyboard
- `/deployments` with diagnosis buttons for failed deployments
- `/investigate <problem>` for a structured investigation that continues until
  findings, evidence, changes, validation, and lessons are reported
- `/nfl_audio <season> <week>` starts a private voice-note collection;
  `/nfl_audio_done` queues schedule-backed, evidence-preserving transcription
  and `/nfl_audio_cancel` discards it. Results are review-only and never become
  official picks without a separate approval.
- `/restart` with interrupt, ten-minute drain, or unlimited-drain choices
- `/schedules`
- `/schedule_add name | cron | prompt`
- `/schedule_pause <id-prefix>`, `/schedule_resume <id-prefix>`
- `/schedule_run <id-prefix>`
- `/chats` lists every chat topic in the Receptionist2 group with its state
- `/instructions [text|skill path|clear]` sets standing instructions that
  every run in the current chat (private chat or topic) follows
- `/tombstone` hands the current chat off to a fresh one (see Tombstones)
- `/knowledge_transfer [focus]` has the current chat propose its reusable
  learnings for `www/knowledge_base` (skill
  `skills/receptionist2-knowledge-transfer/SKILL.md`): which files to add or
  edit and whether topics should be split or merged. After your feedback in
  the same chat it writes them, committing and pushing only that folder. New
  conversations read `knowledge_base/0index.md` during bootstrap, and
  `/tombstone` puts a proposal in the tombstone for the next chat to present.

Watchdog bot:

- `/help`, `/status`, `/logs`, `/restart`
- `/reauth` runs `claude setup-token` in a transient unit outside the
  watchdog sandbox, relays the next plain-text message as the one-time code
  (and deletes it), stores the resulting 1-year token in `auth.env` (see
  Claude authentication) and restarts decision-helper-bot so it loads the
  token. The token carries no profile scope, so sign in to the intended
  Claude account in the browser before authorizing
- `/diagnose` and `/investigate <problem>` start a root diagnostic agent
  outside the watchdog sandbox
- `/model`, `/effort`

Photos and image files (PNG, JPEG, WebP, GIF; usually phone or laptop
screenshots) are saved under `RECEPTIONIST2_STATE_DIR/images` with shared
group-only access and queued as one run whose prompt is the caption plus the
image paths for the agent to open with Read. Images sent together as an album
become a single run. Maintenance removes images after 14 days.

Voice notes, audio files, and audio documents (up to 20 MB and an hour) work
in the private chat and in every topic, as with Mel. The gateway saves them
under `RECEPTIONIST2_STATE_DIR/voice` and transcribes them locally with
faster-whisper (`RECEPTIONIST2_WHISPER_MODEL`, default `small.en`, CPU int8;
the model is cached in `RECEPTIONIST2_STATE_DIR/whisper-models`) in a
short-lived subprocess, one note at a time, so the model is not kept in
memory. Like Mel, it first asks **📝 Transcribe** or **🗑 Trash**; nothing is
transcribed until you choose. The transcript then arrives as a new message
(so Telegram notifies you) in a tap-to-copy block with **▶️ Run** and
**🗑 Trash**: Run queues it as that chat's next prompt; to edit it, tap to
copy, paste into the message box, edit, and send it yourself. Transcriptions interrupted by a gateway restart resume on start.
While a `/nfl_audio` collection is open, private-chat audio goes to it
instead. Maintenance removes voice audio after 14 days.

When the active session's context is nearly full, the gateway offers to start
a new session before continuing.

## Tombstones

`/tombstone` (in the ☰ menu of the private chat and the group) queues a run
in the current chat's own conversation that follows
`skills/receptionist2-tombstone/SKILL.md` and writes everything about the
chat (goal, quoted requirements, decisions, what was done, current state,
open problems, next steps, pending questions, references) to
`/home/receptionist/repos/.receptionist/tombstones/<session>-<time>.md`.
When the file exists the chat offers **➕ Continue in a new chat**: a new
topic named like the old one with a new fruit and `²` (`³`, …), or a new
private session, with the same model, effort, and `/instructions`. Its first
run reads the tombstone and replies `Picked up: … Next: …` without starting
work. The old chat then offers **🗑 Delete this chat** (with confirmation;
refused while it has queued or running runs). That deletes the topic and its
messages, or archives a private session. Tombstone files are kept.

The gateway sets the ☰ command menu for private chats and the group on start.

## Group chats

With `RECEPTIONIST2_GROUP_CHAT_ID` set, a private forum supergroup holds any
number of separate chats, one per topic, each its own Claude conversation.
The bot must be an admin there with Manage Topics and Pin Messages. On start
the gateway creates a 📋 Chats hub topic with a pinned ➕ and hides General.

- Every chat needs a name: text written in 📋 Chats (or General) becomes the
  name of a new topic, "🍓 <name>" with a random fruit, and nothing runs until
  you write inside it. ➕ asks for the name; `/new <name>` requires one. An
  image sent in 📋 Chats is refused until the chat is named.
- Writing in a topic continues that chat. Topics you create yourself are
  adopted and get a fruit added to their name; closing a topic archives its
  chat and writing in it again reopens it. Renaming a topic renames the chat.
- `/stop`, `/status`, `/model`, and `/effort` act on the topic they are sent
  in. Other commands work only in the private chat.
- The private chat stays the receptionist: its runs get a short overview of
  the group's chats, and `/stop` there stops only private-chat runs.
- Chats run in parallel up to `RECEPTIONIST2_MAX_PARALLEL_RUNS`; one chat's
  messages always run in order and resume its conversation. A reply for a
  deleted topic is delivered to the private chat instead.

Schedules use `America/Los_Angeles` unless overridden. Missed occurrences
collapse to one current run because each occurrence has a unique idempotency
key and the scheduler advances directly to the next future fire time.

## Claude authentication

Agent runs, `/diagnose` and `/investigate` agents, and decision-helper-bot
(which shares the `receptionist-agent` login) authenticate with a long-lived `claude setup-token` token stored as
`CLAUDE_CODE_OAUTH_TOKEN` in `/home/receptionist-agent/.claude/auth.env`
(mode 0600). The agent runner loads it for every run; without the file,
Claude falls back to `~/.claude/.credentials.json`, whose refresh token has a
hard expiry after which Claude Code blanks the stored tokens. Watchdog
`/reauth` renews the token: it rewrites only the token line, keeps the
previous file as `auth.env.bak`, and restarts decision-helper-bot, which reads
`auth.env` only at startup. The provider monitor alerts when the token is
rejected or Claude is logged out.

Both bots pass `allowed_updates` explicitly because Telegram otherwise keeps
the last setting a bot polled with; a message-only setting left by an earlier
bootstrap made the watchdog's inline buttons silently do nothing. The watchdog
takes only messages and button presses, so editing a message cannot rerun a
command or be relayed as a reauth code.

## Configuration

Gateway environment:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=...
RECEPTIONIST2_STATE_DIR=/var/lib/receptionist2
RECEPTIONIST2_REPO_ROOT=/home/receptionist/repos
RECEPTIONIST2_TIMEZONE=America/Los_Angeles
RECEPTIONIST2_DEFAULT_MODEL=claude-opus-5-5
RECEPTIONIST2_DEFAULT_EFFORT=high
RECEPTIONIST2_NFL_AUDIO_MAX_BYTES=20971520
RECEPTIONIST2_NFL_AUDIO_MAX_CLIPS=24
RECEPTIONIST2_NFL_AUDIO_MAX_DURATION_SECONDS=3600
RECEPTIONIST2_GROUP_CHAT_ID=-100...   # optional, see Group chats
```

NFL audio is stored under `RECEPTIONIST2_STATE_DIR/nfl-audio` with shared
group-only access while queued. The worker removes raw clips when the run
finishes; interrupted cleanup is retried from durable collection metadata.

Worker environment:

```text
RECEPTIONIST2_STATE_DIR=/var/lib/receptionist2
RECEPTIONIST2_REPO_ROOT=/home/receptionist/repos
CLAUDE_BINARY=/usr/bin/claude
RECEPTIONIST2_AGENT_LAUNCHER=/usr/local/libexec/receptionist2-agent-runner
RECEPTIONIST2_RUN_STATE_DIR=/var/lib/receptionist2/runs
TELEGRAM_ALLOWED_USER_ID=...
RECEPTIONIST2_MAX_PARALLEL_RUNS=2
```

The worker also ingests deployment requests created by
`request-receptionist-deploy` and runs approved ones through
`receptionist-deploy-executor`. The agent runner exports
`RECEPTIONIST2_RUN_ID`, the request records it, and every notice about the
deployment (started, result, Diagnose button) goes to the chat or topic whose
run queued it; requests made outside a run go to the private chat. A request whose command runs
`install-receptionist2` drains running work first and reports its result after
the new worker starts.

Watchdog environment:

```text
WATCHDOG_BOT_TOKEN=...
WATCHDOG_ALLOWED_USER_ID=...
RECEPTIONIST2_STATE_DIR=/var/lib/receptionist2
RECEPTIONIST2_REPO_ROOT=/home/receptionist/repos
RECEPTIONIST2_CONTROL_HELPER=/usr/local/libexec/receptionist2-control
RECEPTIONIST2_DIAGNOSTIC_RUNNER=/usr/local/libexec/receptionist2-diagnostic-runner
```

## Security warning

The operator explicitly approved full-operator agents matching the
forwarder's `claude-channels` bot. `deploy/receptionist2-agent.sudoers` grants
`receptionist-agent` `(ALL) NOPASSWD: ALL`, the installer adds it to the
`root`, `adm`, and `systemd-journal` groups, and the worker unit (whose
namespace every agent run inherits) is unsandboxed.
`deploy/receptionist2-watchdog.sudoers` grants `receptionist2-watchdog`
unrestricted root sudo for `/diagnose`. These apply as soon as the installer
runs, including to agent runs of the existing receptionist.

## Install without starting

```bash
sudo apps/receptionist2/deploy/install-receptionist2 [--revision SHA]
```

The installer archives the exact revision, runs the test suite, and installs
units, helpers, sudoers, the shared workspace `CLAUDE.md` and skills, and the
deployment broker tools (`request-receptionist-deploy`,
`receptionist-deploy-executor`), but enables and starts nothing. `--restart`
restarts only enabled Receptionist2 services. Edit the workspace files in the
repository (`deploy/workspace-CLAUDE.md`, `.claude/skills/`), never in
place: every install overwrites them.

## Cutover

1. Write `/home/receptionist2/.config/receptionist2/gateway.env`,
   `/etc/receptionist2/worker.env`, and `/etc/receptionist2/watchdog.env`
   reusing the current primary and watchdog bot tokens.
2. Run the installer.
3. `systemctl disable --now telegram-receptionist.service
   telegram-receptionist-watchdog.service
   telegram-receptionist-maintenance.timer`
4. `systemctl enable --now receptionist2-worker.service
   receptionist2-gateway.service receptionist2-watchdog.service
   receptionist2-provider-monitor.timer receptionist2-maintenance.timer`
5. Verify `/status` in both bots.

Rollback reverses steps 3 and 4.
