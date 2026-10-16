# Receptionist2

Receptionist2 is a durable Telegram control plane for coding-agent work. It is
implemented separately from the existing receptionist and is installed
disabled by default so both services never poll the same Telegram bot token.

## Services

- `receptionist2-gateway.service`: Telegram intake and outbox delivery
- `receptionist2-worker.service`: durable queued Claude execution
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
- `/restart` with interrupt, ten-minute drain, or unlimited-drain choices
- `/schedules`
- `/schedule_add name | cron | prompt`
- `/schedule_pause <id-prefix>`, `/schedule_resume <id-prefix>`
- `/schedule_run <id-prefix>`

Watchdog bot:

- `/help`, `/status`, `/logs`, `/restart`
- `/reauth` relays the next plain-text message to `claude auth login` (run in
  a transient unit outside the watchdog sandbox) and deletes it
- `/diagnose` and `/investigate <problem>` start a root diagnostic agent
  outside the watchdog sandbox
- `/model`, `/effort`

When the active session's context is nearly full, the gateway offers to start
a new session before continuing.

Schedules use `America/Los_Angeles` unless overridden. Missed occurrences
collapse to one current run because each occurrence has a unique idempotency
key and the scheduler advances directly to the next future fire time.

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
```

Worker environment:

```text
RECEPTIONIST2_STATE_DIR=/var/lib/receptionist2
RECEPTIONIST2_REPO_ROOT=/home/receptionist/repos
CLAUDE_BINARY=/usr/bin/claude
RECEPTIONIST2_AGENT_LAUNCHER=/usr/local/libexec/receptionist2-agent-runner
RECEPTIONIST2_RUN_STATE_DIR=/var/lib/receptionist2/runs
TELEGRAM_ALLOWED_USER_ID=...
```

The worker also ingests deployment requests created by
`request-receptionist-deploy` and runs approved ones through
`receptionist-deploy-executor`. A request whose command runs
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

The operator explicitly approved full-operator agents.
`deploy/receptionist2-agent.sudoers` grants `receptionist-agent` unrestricted
root sudo (every run) and `forwarder` access, and
`deploy/receptionist2-watchdog.sudoers` grants `receptionist2-watchdog`
unrestricted root sudo for `/diagnose`. These apply as soon as the installer
runs, including to agent runs of the existing receptionist. Agent runs still
inherit the worker's read-only system namespace, so root writes must use
`systemd-run`.

## Install without starting

```bash
sudo apps/receptionist2/deploy/install-receptionist2 [--revision SHA]
```

The installer archives the exact revision, runs the test suite, and installs
units, helpers, and sudoers, but enables and starts nothing. `--restart`
restarts only enabled Receptionist2 services; `--workspace` also installs the
shared workspace `CLAUDE.md`, skills, and deployment broker tools.

## Cutover

1. Write `/home/receptionist2/.config/receptionist2/gateway.env`,
   `/etc/receptionist2/worker.env`, and `/etc/receptionist2/watchdog.env`
   reusing the current primary and watchdog bot tokens.
2. Run the installer with `--workspace`.
3. `systemctl disable --now telegram-receptionist.service
   telegram-receptionist-watchdog.service
   telegram-receptionist-maintenance.timer`
4. `systemctl enable --now receptionist2-worker.service
   receptionist2-gateway.service receptionist2-watchdog.service
   receptionist2-provider-monitor.timer receptionist2-maintenance.timer`
5. Verify `/status` in both bots.

Rollback reverses steps 3 and 4.
