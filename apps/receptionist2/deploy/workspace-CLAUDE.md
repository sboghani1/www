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
- Production forwarder operations may run as the trusted `forwarder` account:
  `sudo -n -u forwarder -H bash -lc 'cd /home/forwarder/app && <command>'`.
  Use this for commands that must read or write the authoritative forwarder
  environment or SQLite stores, including MOE opinion generation. Run the
  production checkout's command with `/home/forwarder/venv/bin/python` when it
  needs the forwarder virtual environment. Do not substitute the receptionist
  workspace's Sheet-backed or readonly configuration for a production command.
  Prefer this account over root for forwarder work.
- For analytical questions about completed 2023-2025 NFL games, invoke the
  `nfl-history` skill. It queries the authoritative `nfl_game_history` Sheet tab
  through a fixed read-only helper and reuses a private local cache.
- This agent account has full passwordless root (`sudo -n`). Use it for
  diagnosis and repair, but never as a shortcut around the deployment flow
  below. Never print or expose secret values.
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
- Agent runs and the deployment executor inherit the Receptionist2 worker's
  `ProtectSystem=strict` mount namespace. Any command that writes to `/opt`,
  `/etc`, `/usr/local`, or otherwise changes system services (including
  `sudo` commands) must launch a transient systemd unit with `systemd-run` so
  it runs outside that read-only namespace. Add `--wait --pipe` when the result
  must reflect the command's success and include its output.
- To deploy Receptionist2 itself, queue exactly:
  `systemd-run --unit=receptionist2-deploy-$(date +%s) --collect /bin/bash /home/receptionist/repos/www/apps/receptionist2/deploy/install-receptionist2 --restart`
  Receptionist2 drains running work before its own restart and reports the
  result after it comes back.
- For `telegram-channel-forwarder` changes that affect the NFL intake bot, run
  its tests, commit and push a clean `main`, then queue the fixed deployment:
  `request-telegram-intake-deploy --summary "<what changed>"`.
  Do not SSH to root or run `git pull` and `systemctl restart` directly.
- The service namespace intentionally sees `/` as read-only. Never infer a
  host filesystem incident from `findmnt` or a write test inside it. Check the
  real host with
  `sudo -n systemd-run --pipe --wait --quiet findmnt -no OPTIONS /`.
- `errors=remount-ro` is a normal ext4 safety option even while the filesystem
  is healthy and mounted `rw`; it is not evidence of an error by itself.
- WNBA installer requests must use:
  `systemd-run --unit=wnba-poller-install-$(date +%s) --collect --wait --pipe /bin/bash /home/receptionist/repos/www/apps/wnba-poller/deploy/install-wnba-poller --enable-timers --enable-guesser-bot`.
