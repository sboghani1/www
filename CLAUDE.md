# www monorepo

## Telegram receptionist

The application lives in `apps/telegram-receptionist`.

Run its tests before committing:

```bash
cd apps/telegram-receptionist && python -m pytest
```

Production deployment must be proposed through the Telegram approval broker
after committing and pushing a clean revision:

```bash
request-receptionist-deploy \
  --repo /home/receptionist/repos/www \
  --summary "Deploy the Telegram receptionist" \
  --command 'systemd-run --unit=telegram-receptionist-deploy-$(date +%s) --collect /usr/local/libexec/deploy-telegram-receptionist-worker'
```

The bot displays the exact revision and command. Deployment occurs only after
the authorized user approves that immutable request in Telegram. The agent has
no direct sudo permission.

The approval executor runs inside the receptionist service's read-only system
mount namespace. Root commands that write under `/opt`, `/etc`, `/usr/local`,
or manage systemd units must use `systemd-run` to launch a transient unit
outside that namespace. Add `--wait` when approval should report the detached
installer's final result and `--pipe` when its actual logs are needed.

The service namespace being read-only does not mean the host filesystem is
read-only. Use
`sudo -n /usr/local/libexec/receptionist-host-recovery diagnose` from the
agent account before diagnosing a host mount incident.
