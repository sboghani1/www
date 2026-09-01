# Multi-repository workspace

This directory is the root of a private coding workspace.

- Work across any existing repository beneath this directory without asking the
  user to select or approve a repository.
- Clone additional repositories beneath this directory when the task requires
  them.
- Before changing a repository, read and follow its own `CLAUDE.md`,
  `AGENTS.md`, and `.github/copilot-instructions.md` files when present.
- Keep unrelated repositories unchanged.
- Use the Git identity already configured for this Unix user.
- Google Sheets credentials are available through `GOOGLE_CREDENTIALS`,
  `GOOGLE_SERVICE_ACCOUNT_JSON`, and `NFL_INTAKE_SHEET_ID`. Never print or
  expose their values.
- A small environment with `gspread` and `google-auth` is available at
  `/home/receptionist-agent/.cache/google-sheet-check`; repositories may create
  their own virtual environments when they need additional dependencies.
- For analytical questions about completed 2023-2025 NFL games, invoke the
  `nfl-history` skill. It queries the authoritative `nfl_game_history` Sheet tab
  through a fixed read-only helper and reuses a private local cache.
- Never run a production deployment directly. Queue one immutable request only
  after the repository is clean, pushed, tested, and the exact command is ready:

  ```bash
  request-receptionist-deploy \
    --repo /home/receptionist/repos/<repo> \
    --summary "<what will be deployed>" \
    --command "<exact root command to execute on pickbot>"
  ```

- The bot automatically executes a valid request and displays the repository,
  exact revision, summary, full command, and result. Requests are one-use and
  expire after 15 minutes.
- The command executes as root on the `pickbot` VPS, so commands should
  target local production paths directly rather than SSHing back into the same
  server.
- The deployment executor inherits the receptionist service's
  `ProtectSystem=strict` mount namespace. Any command that writes to `/opt`,
  `/etc`, `/usr/local`, or otherwise changes system services must launch a
  detached transient systemd unit so it runs outside that read-only namespace.
- For the receptionist itself, the detached command is:
  `systemd-run --unit=telegram-receptionist-deploy-$(date +%s) --collect /usr/local/libexec/deploy-telegram-receptionist-worker`
- For `telegram-channel-forwarder` changes that affect the NFL intake bot, run
  its tests, commit and push a clean `main`, then queue the fixed deployment:
  `request-telegram-intake-deploy --summary "<what changed>"`.
  The requester binds the immutable workspace revision to a root worker that
  only fast-forwards `/home/forwarder/app` to that exact revision and restarts
  `telegram-intake.service` once. Do not SSH to root or run `git pull` and
  `systemctl restart` directly.
- Use the same pattern for other root installers, with a unique unit name and
  `--wait --pipe` when the deployment result must reflect installer success or
  failure and include its actual output.
- The receptionist service intentionally sees `/` as read-only. Never infer a
  host filesystem incident from `findmnt` or a write test in the service/agent
  namespace. Run the fixed read-only diagnostic instead:
  `sudo -n /usr/local/libexec/receptionist-host-recovery diagnose`.
- `errors=remount-ro` is a normal ext4 safety option even while the filesystem
  is healthy and mounted `rw`; it is not evidence of an error by itself.
- If diagnostics prove the host root is actually `ro`, create a normal
  immutable deployment request for this exact guarded repair:
  `systemd-run --unit=receptionist-root-recovery-$(date +%s) --collect --wait --pipe /usr/local/libexec/receptionist-host-recovery remount-root-rw`.
  The helper refuses to remount when kernel filesystem/I/O errors are present;
  that case requires provider recovery and offline `fsck`.
- WNBA installer requests must use:
  `systemd-run --unit=wnba-poller-install-$(date +%s) --collect --wait --pipe /bin/bash /home/receptionist/repos/www/apps/wnba-poller/deploy/install-wnba-poller --enable-timers --enable-guesser-bot`.
