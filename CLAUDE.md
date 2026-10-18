# www monorepo

## Telegram receptionist

The application lives in `apps/telegram-receptionist`.

Run its tests before committing:

```bash
cd apps/telegram-receptionist && python -m pytest
```

Production deployment must be queued through the receptionist deployment broker
after committing and pushing a clean revision:

```bash
request-receptionist-deploy \
  --repo /home/receptionist/repos/www \
  --summary "Deploy the Telegram receptionist" \
  --command 'systemd-run --unit=telegram-receptionist-deploy-$(date +%s) --collect /usr/local/libexec/deploy-telegram-receptionist-worker'
```

The bot automatically executes a valid immutable request and reports the exact
revision, command, and result in Telegram. Create a request only when the exact
root command is ready to run. The agent has no direct sudo permission.

The deployment executor runs inside the receptionist service's read-only system
mount namespace. Root commands that write under `/opt`, `/etc`, `/usr/local`,
or manage systemd units must use `systemd-run` to launch a transient unit
outside that namespace. Add `--wait` when approval should report the detached
installer's final result and `--pipe` when its actual logs are needed.

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
