# www monorepo

## Telegram receptionist

The application lives in `apps/telegram-receptionist`.

Run its tests before committing:

```bash
cd apps/telegram-receptionist && python -m pytest
```

The live receptionist may deploy itself only after committing and pushing a
clean `main` branch:

```bash
sudo -n /usr/local/sbin/deploy-telegram-receptionist
```

That command is a restricted root-owned launcher. Do not attempt to modify the
installed launcher, deployment worker, sudoers policy, or systemd unit from an
agent session.

