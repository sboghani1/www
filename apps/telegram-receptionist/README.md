# Telegram Receptionist

Private Telegram bot that runs resumable coding-agent turns on a VPS.

## Current provider

Claude Code is invoked in headless mode. Each Telegram message is passed as the
exact prompt argument, and the returned Claude session ID is persisted for the
next turn.

## Commands

- `/start`, `/help`, `/status`
- `/repos`
- `/new [name]`, `/sessions`, `/switch <session-id-prefix>`, `/reset`
- `/provider`, `/verbose [on|off]`
- `/stop`
- `/approve <request-id>`, `/deny <request-id>`, `/deployments`

Plain text is queued as the exact next agent message. V1 accepts text only.

## Local development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest
```

## Required environment

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=123456789
RECEPTIONIST_REPO_ROOT=/home/receptionist/repos
RECEPTIONIST_STATE_DIR=/var/lib/telegram-receptionist
CLAUDE_BINARY=/usr/bin/claude
AGENT_LAUNCHER=/usr/local/libexec/receptionist-agent-runner
AGENT_KILLER=/usr/local/libexec/receptionist-agent-killer
AGENT_TIMEOUT_SECONDS=3600
MAX_QUEUED_MESSAGES=10
```

Secrets belong only in the VPS environment file. Do not commit them.

All sessions start at `/home/receptionist/repos` and may work across any
repository beneath it. `/repos` is informational; no repository registration or
switching is required.

The bot runs as `receptionist`; Claude and Git run as `receptionist-agent`
through a fixed root-owned launcher. This separation keeps the Telegram token
and bot state outside the coding agent's Unix permissions.

The launcher may load only these Google Sheets values from the agent-owned
`~/.config/receptionist-agent/google.env`:

- `GOOGLE_CREDENTIALS`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `NFL_INTAKE_SHEET_ID`

It does not load the forwarder's general environment.

## Deployment approvals

Agents submit deployment proposals with `request-receptionist-deploy`. The bot
shows the exact repository revision and root command in Telegram. A request:

- executes only after `/approve <request-id>`
- can execute once
- cannot be edited after display
- expires after 15 minutes
- is denied permanently with `/deny <request-id>`

The agent user has no direct root deployment permission.
