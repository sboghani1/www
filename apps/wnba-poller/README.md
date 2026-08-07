# WNBA Poller

Python 3.12 service for the `asce Guesser` workbook. It maintains a rolling
14-day ESPN WNBA schedule, polls BetOnline full-game and first-half lines, and
freezes metadata on thoughts entered directly in Google Sheets or through the
dedicated private `@wnbaguesser_bot`.

The runtime is self-contained. Files in `source_artifacts/` are byte-exact
migration evidence and are never imported. `path210.md` is the active curated
decision log; Sheet thoughts do not rewrite it automatically.

## Environment

Store these values only in the restricted runtime environment file:

```text
WNBA_SHEET_ID=...
ODDS_API_KEY=...
GOOGLE_CREDENTIALS=...              # base64 service-account JSON
# or:
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/restricted-service-account.json
```

Optional HTTP controls:

```text
WNBA_HTTP_TIMEOUT_SECONDS=20
WNBA_HTTP_RETRIES=2
```

The bot has a separate mode-0600 environment file:

```text
WNBA_GUESSER_BOT_TOKEN=...
WNBA_GUESSER_EXPECTED_USERNAME=wnbaguesser_bot
WNBA_GUESSER_SHEET_TIMEOUT_SECONDS=45
WNBA_SHEET_ID=...
GOOGLE_CREDENTIALS=...
# or GOOGLE_SERVICE_ACCOUNT_JSON=...
```

Commands validate required variable names and never print their values.
The configured Sheet ID must resolve to a workbook titled `asce Guesser`.

## Local development

```bash
PYENV_VERSION=3.12.11 python -m venv .venv && .venv/bin/pip install -e '.[test]' && PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider
```

Tests are offline. No live network or Google Sheet tests run by default.

## CLI

```bash
wnba-poller sync-schedule
wnba-poller poll-odds
wnba-poller reconcile-thoughts
wnba-poller status
wnba-poller status --json
```

`poll-odds` reads `wnba_games` first and makes no Odds API request when no
game is due. Games more than six hours from tip are due hourly; games at or
inside six hours are due every 15 minutes; started games are never due.

### Workbook initialization

Non-destructive initialization creates missing tabs and validates existing
headers. Seed the initial WNBA-only Sheet allowlist with the authorized user's
numeric Telegram ID:

```bash
wnba-poller initialize-sheet \
  --seed-allowed-user-id <numeric-user-id> \
  --seed-allowed-username <username> \
  --seed-allowed-display-name "<display name>"
```

Replacing WNBA tabs or removing the copied NFL tabs requires both an explicit
confirmation flag and a new JSON backup path:

```bash
wnba-poller initialize-sheet --confirm-replace-tabs --remove-legacy-nfl-tabs --backup ./asce-guesser-before-wnba.json
```

The destructive command backs up every worksheet, including formulas, before
deleting anything. Backup files can contain private workbook data and must not
be committed.

## Sheet schema

- `wnba_games`: ESPN identity/schedule fields, Odds API identity, normalized
  opening/latest full-game and first-half fields, poll timestamps, and reserved
  `manual_status`, `user_notes`, and `user_tags` columns.
- `wnba_line_snapshots`: normalized append-only lines plus UTC/ET capture time
  and API quota headers.
- `wnba_thoughts`: immutable ID and UTC/ET submission time, source/Telegram
  metadata, selected period/market/side, selected opening/latest price,
  frozen game/latest-line fields, exact thought text, and manual
  `processed_into_path210_at` bookkeeping.
- `wnba_allowed_users`: WNBA-only numeric Telegram allowlist. Only rows with a
  positive integer ID and enabled value are accepted. Initialization can add
  the first user idempotently; later access changes are direct Sheet edits.
- `wnba_settings`: key/value diagnostics and last-success timestamps.

All machine writes use Google Sheets `RAW` input. Thought reconciliation reads
formula text with `value_render_option=FORMULA`, then writes it as raw text, so
a leading `=`, `+`, `-`, or `@` is preserved instead of executed. Existing
thought text is never edited.

The WNBA Guesser calls `SheetsStore.append_thought_record(...)` directly in the
same package and Unix identity as the poller. The core method freezes the latest
game/line metadata, writes raw text, and treats either `thought_id` or the
Telegram chat/message pair as an idempotency key.

Schedule sync only upserts. An ESPN error occurs before any Sheet write, and
missing ESPN events never cause existing rows to be deleted. Opening values are
filled from the first available market and never replaced; latest values always
reflect the successful poll. Changed snapshots append immediately, while an
identical snapshot appends only after 60 minutes.
The CLI records quota headers and emits a journald-visible warning at 50
remaining requests or fewer.

Historical text logs are preserved under `source_artifacts/`. Automated history
import is intentionally omitted because the legacy rows have no immutable event
IDs or absolute capture timestamps; importing them without a reviewed fixture
mapping would create false structured history.

## WNBA Guesser bot

`@wnbaguesser_bot` is a standalone WNBA-only Bot API process. It does not share
commands, state, token, deployment, or failure modes with the general Telegram
receptionist or the NFL bot.

Commands:

- `/wnba`: browse the next 14 days in Eastern time.
- `/wnba_thoughts`: list the 10 most recent append-only entries.
- `/wnba_cancel`: discard the active in-memory selection.

The NFL-style flow is game → full game/first half → market → side → exact
reasoning. Full game offers spread, moneyline, and total; first half offers
spread and total. Every screen shows opening and latest BetOnline values.
Reasoning is stored character-for-character as Telegram supplies it, with `RAW`
Sheet input. A deterministic `telegram:<chat_id>:<message_id>` key makes
retries idempotent.

After a successful submission, `Add another update` repeats the same structured
selection against freshly read lines and appends a new row. Earlier reasoning
is never overwritten. A failed/unconfirmed write retains the original exact
text and submission ID in the active flow so the same text retries safely.
Selections expire after 15 minutes or on process restart; `/wnba_cancel`
clears them immediately.

Every command, callback, and text handler requires a private chat and checks
the numeric ID against `wnba_allowed_users` before reading games or thoughts.
The allowlist is cached for at most 60 seconds. Callback payloads contain only
short action tokens and a bounded immutable event ID and stay below Telegram's
64-byte limit.

Separation is deliberate: a dedicated bot token and service avoid polling
conflicts, keep WNBA UX/state independent, and let bot failures or restarts
leave the coding receptionist and NFL intake untouched. Because the bot and
poller use the same Sheet package, running both as `receptionist-agent` in one
root-owned venv is simpler and no less isolated from unrelated bot tokens.

## systemd installation

The installer is root-only, builds and tests a root-owned release, installs the
hardened units, and does not enable timers unless explicitly requested:

```bash
sudo apps/wnba-poller/deploy/install-wnba-poller
sudoedit /home/receptionist-agent/.config/wnba-poller/env
sudoedit /home/receptionist-agent/.config/wnba-guesser/env
sudo -u receptionist-agent /opt/wnba-poller/current/bin/wnba-poller \
  initialize-sheet --seed-allowed-user-id <numeric-user-id>
sudo /usr/local/sbin/install-wnba-poller \
  --enable-timers --enable-guesser-bot
```

From the receptionist automatic deployment broker, run the installer outside the
read-only service namespace and pipe its real output back:

```bash
systemd-run --unit=wnba-poller-install-$(date +%s) \
  --collect --wait --pipe \
  /bin/bash /home/receptionist/repos/www/apps/wnba-poller/deploy/install-wnba-poller \
  --enable-timers --enable-guesser-bot
```

Both jobs run as `receptionist-agent` and share one nonblocking `flock`. The
15-minute service polls due odds and reconciles direct-Sheet thoughts. The
schedule timer runs daily at 5:20 AM America/New_York. Services use a 96 MB
memory high-water mark and 160 MB hard limit. The independent
`wnba-guesser-bot.service` also runs as `receptionist-agent`, reads only its
dedicated environment file, and uses 128 MB/192 MB memory limits.

## Troubleshooting `/wnba` game-loading errors

Two independent bots intentionally use `/wnba` and the same generic failure
text:

- `@ascereceptionist_bot` runs `telegram-receptionist.service` and calls the
  short-lived `/usr/local/libexec/receptionist-wnba-helper`.
- `@wnbaguesser_bot` runs the long-lived `wnba-guesser-bot.service` directly
  from `/opt/wnba-poller/current`.

Always identify which bot received the command before diagnosing. Search the
matching unit's journal, then compare the running process command with
`readlink -f /opt/wnba-poller/current`. A release symlink can be current while
an old Python process remains resident.

The August 7, 2026 incident in `@wnbaguesser_bot` was exactly that condition:
the process still loaded the August 5 release and rejected the newer
`wnba_games` Sheet headers. Restarting `wnba-guesser-bot.service` loaded the
current release and restored a 46-game Sheet read. The installer now explicitly
restarts the Guesser after switching the release symlink and verifies the unit
is active; `systemctl enable --now` alone is not a restart for an already
running service.

## Validation

Offline Python 3.12 tests:

```text
35 passed in 3.11s
```

Coverage includes game/period/market/side navigation, callback bounds,
Sheet-backed authorization, exact/repeatable/idempotent reasoning, failure-safe
retry state, and the existing schedule/odds/Sheet behavior. No bot start,
deployment, live Sheet mutation, or live network validation was performed.
