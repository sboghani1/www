# www monorepo

## Knowledge base

`knowledge_base/` holds reusable, general understanding of this repo and its
infrastructure. Read `knowledge_base/0index.md` first and then the topics
relevant to your task. Record new reusable learnings there by following
`apps/receptionist2/skills/receptionist2-knowledge-transfer/SKILL.md`: propose
the files to add or edit and wait for the user's feedback on topic boundaries
before writing. The index's writing rules apply, and this repository is
public.

## Receptionist2

The private `@ascereceptionist_bot` application lives in `apps/receptionist2`
(see its README). The original receptionist in `apps/telegram-receptionist` is
retired: its bot units were removed, and that directory is kept only because
its installed environment at `/opt/telegram-receptionist/current/.venv` still
hosts shared tools (nfl-audio-parser, nfl-history, WNBA helpers,
`receptionist-host-recovery`). Do not deploy or restart the old bot.

Run its tests before committing:

```bash
cd apps/receptionist2 && python -m pytest
```

Production deployment must be queued through the receptionist deployment broker
after committing and pushing a clean revision:

```bash
request-receptionist-deploy \
  --repo /home/receptionist/repos/www \
  --summary "Deploy Receptionist2" \
  --command 'systemd-run --unit=receptionist2-deploy-$(date +%s) --collect /bin/bash /home/receptionist/repos/www/apps/receptionist2/deploy/install-receptionist2 --restart'
```

Do not add `--wait` to this one: the install restarts the worker that runs
the request. Receptionist2 drains running work first and reports the result
after it comes back.

Receptionist2 executes a valid immutable request automatically and reports the
exact revision, command, and result in the chat whose run queued it. Create a
request only when the exact root command is ready to run.

The deployment executor runs inside a read-only system mount namespace. Root
commands that write under `/opt`, `/etc`, `/usr/local`, or manage systemd
units must use `systemd-run` to launch a transient unit outside that
namespace. Add `--wait` when the result should report the installer's final
status and `--pipe` when its actual logs are needed.

The service namespace being read-only does not mean the host filesystem is
read-only. Use
`sudo -n /usr/local/libexec/receptionist-host-recovery diagnose` from the
agent account before diagnosing a host mount incident.

## Telegram clipboard

The private `@clipboard2_bot` application lives in
`apps/telegram-clipboard`. It persistently accepts text, images, documents, and
videos from the allowlisted private chat. Receptionist2 exposes the latest item
as untrusted user data in startup context.

Run its tests before committing:

```bash
cd apps/telegram-clipboard && python -m pytest
```

Install or update it through a transient unit:

```bash
systemd-run --unit=telegram-clipboard-install-$(date +%s) --collect --wait --pipe \
  /bin/bash /home/receptionist/repos/www/apps/telegram-clipboard/deploy/install-telegram-clipboard
```

Use `clipboard-send` only after the user approves the exact outbound content.

## Mel decision helper

The private `@decision_helpers_bot` ("Mel") application lives in
`apps/decision-helper-bot`. It transcribes NFL/NBA pick voice notes locally
with faster-whisper, extracts and reviews picks through text-only Claude Code
calls on the receptionist-agent subscription login, and grades them with ESPN.
Input notes get a confirmation only. Reviews are stored silently. `/games`
gives Mel's opinion on a selected game.
Lines are read from telegram-channel-forwarder's `nfl_games` / `nba_games`
Sheet tabs (Bovada since 2026-10-27). Mel's only Odds API uses are a
BetOnline/Bovada/Hard Rock snapshot 60 minutes before each NBA tip and a
BetOnline fallback for bait-board games Bovada hasn't posted (read at midnight
and 3 PM Pacific)
(see its README); nothing else spends credits.

Run its tests before committing:

```bash
cd apps/decision-helper-bot && python -m pytest
```

Install or update it through a transient unit:

```bash
systemd-run --unit=decision-helper-bot-install-$(date +%s) --collect --wait --pipe \
  /bin/bash /home/receptionist/repos/www/apps/decision-helper-bot/deploy/install-decision-helper-bot
```
