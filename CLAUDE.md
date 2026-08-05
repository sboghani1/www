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
