# Receptionist2

Verified: 2026-11-09 (code in `apps/receptionist2`, production release
`4c20bf6`).

Private, single-user Telegram control plane for Claude Code agent work. Full
operator detail is in `apps/receptionist2/README.md`; this is the mental model.

## Processes

- **Gateway** (`receptionist2-gateway.service`, user `receptionist2`):
  Telegram intake, outbox delivery, progress edits, schedules. Never runs
  agents.
- **Worker** (`receptionist2-worker.service`): claims queued runs and launches
  `claude -p --output-format stream-json` as `receptionist-agent` through the
  sudo-only agent runner, with cwd `/home/receptionist/repos`.
- **Watchdog** (separate bot): deterministic controls that work when the main
  bot is broken: `/status`, `/logs`, `/restart`, `/reauth`, `/diagnose`.
- Gateway and worker talk only through SQLite
  (`/var/lib/receptionist2/receptionist2.db`): `runs` is the queue, `outbox`
  holds deliveries that are retried until Telegram accepts them. Restarting
  the gateway never kills a run.

## Runs and sessions

- A session is one Claude conversation (`sessions.provider_session_id`,
  resumed with `--resume`). Each run copies the session's latest conversation
  id when it is claimed, so a message queued while the previous one ran still
  continues the same conversation.
- Up to `RECEPTIONIST2_MAX_PARALLEL_RUNS` (2) runs execute at once, but never
  two from one session. Parallel runs share one repository checkout, so two
  chats editing the same repo at once can collide.
- A run with no conversation yet gets the bootstrap context manifest
  (repositories, schedules, clipboard, active session) and the bootstrap
  skill.

## Chats

- **Private chat**: one active session, switchable with `/new`, `/sessions`,
  `/switch`. Its runs get an overview of the group's chats.
- **Group chats**: a private forum supergroup (`RECEPTIONIST2_GROUP_CHAT_ID`);
  each topic is its own session. A hub topic "📋 Chats" with a pinned ➕ is
  where new chats are named; text written there becomes a topic called
  "<random fruit> <name>". Group updates never touch `user_state`, which
  belongs to the private chat. Commands in a topic act on that topic.
- `/instructions <text or skill path>` stores standing instructions on the
  session; the worker prepends them to every run's prompt. This is how a chat
  becomes a specialist (for example one that follows a parsing skill).

## Inputs

- **Images** (photos, image documents, albums): saved under
  `/var/lib/receptionist2/images`; the prompt lists the paths for the agent's
  Read tool.
- **Voice/audio**: transcribed locally with faster-whisper `small.en` (CPU
  int8) in a short-lived subprocess, one at a time, so the ~0.5 GB model is not
  held in memory. The transcript is shown with Run/Trash buttons; Run queues
  it as the chat's next prompt.

## Tombstones

`/tombstone` makes the chat write a handoff file
(`/home/receptionist/repos/.receptionist/tombstones/`) per
`skills/receptionist2-tombstone/SKILL.md`. "Continue in a new chat" opens a
fresh chat (same name, new fruit, ² ³ …) that inherits model, effort, and
instructions and resumes from the file. The old chat can then delete itself.
Tombstones carry session state; reusable lessons go to this knowledge base.
A tombstone includes a pending knowledge-transfer proposal that the next chat
presents for the user's feedback before anything is written here.

## Knowledge transfer

`/knowledge_transfer [focus]` runs
`skills/receptionist2-knowledge-transfer/SKILL.md` in the current chat. It
proposes files to add or edit and topic splits or merges, waits for the
user's feedback, then writes, commits, and pushes only `knowledge_base/`.

## Deploying

Commit and push to `main`, check that no run is queued or running, then on
the VPS pull the `www` checkout as `receptionist-agent` and run
`apps/receptionist2/deploy/install-receptionist2 --restart` as root. The
installer builds a release under `/opt/receptionist2/releases/<sha>`, runs
the test suite, installs units and sudoers, and restarts all three services.
Agents inside Receptionist2 must instead queue deploys through
`request-receptionist-deploy` (see `CLAUDE.md`). The agent runner exports
`RECEPTIONIST2_RUN_ID`; the request records it, so the deployment's
started/result notices and its Diagnose button appear in the chat or topic
that queued it (requests made outside a run report to the private chat).

## Gotchas

- Tests use `SimpleNamespace` configs and fake bots; they cannot catch
  sandbox or Telegram-permission failures. Verify new file-system or Telegram
  behaviour on the VPS (see systemd-sandboxing.md).
- Bot-created forum-topic service messages arrive from the bot itself and are
  rejected by the single-user authorization check; that is expected.
