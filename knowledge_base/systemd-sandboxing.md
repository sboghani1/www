# systemd sandboxing pitfalls

Verified: 2026-11-09 (Ubuntu 26.04 VPS; Receptionist2 gateway unit).

## Options that break ordinary code

- **`RestrictSUIDSGID=true`** makes `mkdir`/`chmod` with a set-gid or set-uid
  bit fail with `EPERM` ("Operation not permitted"), even on directories the
  process owns. Code that creates shared directories with mode `0o2770`
  breaks. Use `0o770` and let the unit's `Group=` give new files the shared
  group, or have a root installer create the set-gid directory in advance
  (`install -d -m 2770 -o <owner> -g <group> <dir>`).
- **`ProtectHome=read-only`** with `HOME` set: libraries that cache under
  `~/.cache` (Hugging Face, pip, and similar) fail. Point their cache variables
  (for example `HF_HOME`) at a `ReadWritePaths=` directory.
- **`ProtectSystem=strict`**: everything outside `ReadWritePaths=` is
  read-only inside the unit. That says nothing about the host; do not
  diagnose a read-only host filesystem from inside a sandboxed service.
- **`PrivateTmp=true`**: `/tmp` is private to the unit, so files written there
  are invisible to other processes.

## Reproducing a unit's sandbox

Unit tests run unsandboxed and miss all of the above. Reproduce with the same
properties:

```bash
systemctl show <unit> -p User -p Group -p RestrictSUIDSGID -p ProtectHome -p ProtectSystem
systemd-run --quiet --wait --pipe -p User=<user> -p Group=<group> \
  -p ProtectSystem=strict -p ProtectHome=read-only \
  -p ReadWritePaths=<dir> -p RestrictSUIDSGID=yes -p UMask=0007 \
  <command>
```

## Checking another user's access

`sudo -u <user> /usr/bin/test -r <path>` can report failure for files that
user can read (it was observed returning 1 while `cat` succeeded). Check with
the real operation instead: `sudo -u <user> cat <file>` or
`sudo -u <user> bash -c '[ -r <path> ]'`.
