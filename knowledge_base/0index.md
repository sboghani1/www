# Knowledge base index

Reusable, evidence-backed understanding of how things in this repo and its
infrastructure work, shared by every agent and chat. Read this index first,
then only the topics relevant to the task and their useful connections.

## Writing rules

- **General, not diaries.** Write what is true and reusable ("X rejects Y
  because Z; do W"), not what happened in one session. Session history belongs
  in tombstones, not here.
- **Update before adding.** Extend or correct an existing topic when one fits;
  create a new topic only for a genuinely separate subject, and add it below.
- **Evidence and dates.** Say how each non-obvious claim was verified (file,
  command, observed behaviour) and keep a `Verified:` date per topic. Mark
  uncertain claims as such, and fix or remove claims found to be wrong.
- **This repository is public.** Never write secrets, tokens, credentials,
  IP addresses, account emails, Telegram user or chat IDs, or exploitable
  security details. Say where such things live instead.
- **Concise.** Short sections, exact names (paths, units, commands), no
  narration.
- **Propose, then write.** Use the `receptionist2-knowledge-transfer` skill
  (`apps/receptionist2/skills/receptionist2-knowledge-transfer/SKILL.md`;
  `/knowledge_transfer` runs it from any Receptionist2 chat). It first
  proposes the files to add or edit and how topics should be split or
  merged; nothing is written until the user gives feedback.

## Topics

Format: `file` — what it covers. *Connections:* related topics.

- `receptionist2.md` — Receptionist2 architecture: gateway, worker,
  watchdog, SQLite queue and outbox, private chat and forum-topic chats,
  parallel runs, images, voice notes, `/instructions`, tombstones, deploying.
  *Connections:* claude-code-auth.md (agent login), telegram-bot-api.md
  (Telegram behaviour it works around), systemd-sandboxing.md (unit
  restrictions), vps-operations.md (where it runs).
- `claude-code-auth.md` — Claude Code subscription auth for headless
  services: `claude setup-token`, refresh-token hard expiry, `auth.env`,
  verifying which account a token belongs to. *Connections:* receptionist2.md
  (`/reauth`), vps-operations.md.
- `telegram-bot-api.md` — Telegram Bot API behaviour that surprises:
  remembered `allowed_updates`, forum topics, pins, edits, menus, desktop
  client automation limits. *Connections:* receptionist2.md.
- `systemd-sandboxing.md` — How systemd hardening options break ordinary
  code (`RestrictSUIDSGID`, `ProtectHome`, `ProtectSystem`) and how to
  reproduce a unit's sandbox. *Connections:* receptionist2.md,
  vps-operations.md.
- `vps-operations.md` — The production VPS: service accounts, checkouts,
  safe deploys, pushing to GitHub, editing env files, rendering images with
  headless Chrome. *Connections:* receptionist2.md, systemd-sandboxing.md,
  claude-code-auth.md, sports-data-sources.md.
- `sports-data-sources.md` — NFL schedules, scores, lines and starters: ESPN
  scoreboard and `pickcenter` (open/close/live), box-score starting QBs, the
  forwarder's `nfl_line_snapshots` tab and its cadence, book differences.
  *Connections:* youtube-pick-extraction.md, capper-backtesting.md,
  vps-operations.md.
- `youtube-pick-extraction.md` — Turning a YouTube picks show into graded
  picks: listing, proxied download, AssemblyAI speaker labels, subagent
  extraction with verbatim quotes, cross-checks, grading gotchas.
  *Connections:* sports-data-sources.md, capper-backtesting.md,
  vps-operations.md.
- `capper-backtesting.md` — Testing a handicapper's patterns honestly:
  closing-line grading, walk-forward, season halves, shuffle tests,
  second-book replication, multiple testing. *Connections:*
  sports-data-sources.md, youtube-pick-extraction.md.
