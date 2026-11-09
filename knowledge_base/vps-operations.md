# Production VPS operations

Verified: 2026-11-09.

One Ubuntu VPS (clock America/New_York) runs the Telegram bots and the
pluribus forwarder. Connection details are kept outside this public repo.

## Accounts and checkouts

- `receptionist-agent`: the account Claude agents run as; full passwordless
  sudo. Owns the shared workspace `/home/receptionist/repos` (including the
  production `www` checkout) and `~/.claude` (Claude login, `auth.env`).
- `receptionist2`, `receptionist2-worker`: Receptionist2 gateway and worker
  service users, sharing group `receptionist2-state` for
  `/var/lib/receptionist2`.
- `forwarder`: the pluribus telegram-channel-forwarder; it uses a different
  person's Claude account. Never mix the two accounts' credentials.
- Root git commands in the `www` checkout need
  `git -c safe.directory=/home/receptionist/repos/www ...`; prefer running git
  as the owner: `sudo -n -u receptionist-agent -H git -C /home/receptionist/repos/www pull --ff-only`.

## Safe deploys

- Before restarting a service that runs agents, check that nothing is queued
  or running (Receptionist2: `runs.status IN ('queued','running')`).
- Install through the app's installer script (it tests, then restarts) rather
  than restarting units by hand after a code change.
- After deploying, check `systemctl is-active` and the last minute of
  `journalctl -u <unit>` for warnings, and exercise the changed path for real.

## Environment files

Edit without breaking: copy to a timestamped backup with `cp -p`, write the new
content to a temp file in the same directory, `chown`/`chmod` it to match, and
`mv` it over the original; append or replace only the intended line, then
`diff` against the backup.

## GitHub

`sboghani1/www` is public. Pushing from a machine whose default `gh` account
is a different (enterprise) user needs a one-off credential helper for
`sboghani1` rather than changing global git config:

```bash
git -c credential.helper= \
  -c 'credential.helper=!f() { [ "$1" = get ] || return 0; echo username=sboghani1; echo "password=$(gh auth token --user sboghani1)"; }; f' \
  push origin HEAD:main
```
