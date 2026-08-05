# Telegram Receptionist

Private Telegram bot that runs resumable coding-agent turns on a VPS.

## Current provider

Claude Code is invoked in headless mode. Each Telegram message is passed as the
exact prompt argument, and the returned Claude session ID is persisted for the
next turn.

## Commands

- `/start`, `/help`, `/status`
- `/repos`, `/repo <name>`
- `/new [name]`, `/sessions`, `/switch <session-id-prefix>`, `/reset`
- `/provider`, `/verbose [on|off]`
- `/stop`

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
RECEPTIONIST_REPOSITORIES=www:/home/receptionist/repos/www,agent-sandbox:/home/receptionist/repos/agent-sandbox
RECEPTIONIST_STATE_DIR=/var/lib/telegram-receptionist
CLAUDE_BINARY=/usr/bin/claude
AGENT_LAUNCHER=/usr/local/libexec/receptionist-agent-runner
AGENT_KILLER=/usr/local/libexec/receptionist-agent-killer
AGENT_TIMEOUT_SECONDS=3600
MAX_QUEUED_MESSAGES=10
```

Secrets belong only in the VPS environment file. Do not commit them.

The bot runs as `receptionist`; Claude and Git run as `receptionist-agent`
through a fixed root-owned launcher. This separation keeps the Telegram token
and bot state outside the coding agent's Unix permissions.
