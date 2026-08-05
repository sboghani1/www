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
- Never run a production deployment directly. Propose one immutable request:

  ```bash
  request-receptionist-deploy \
    --repo /home/receptionist/repos/<repo> \
    --summary "<what will be deployed>" \
    --command "<exact root command to execute on pickbot>"
  ```

- The bot displays the repository, exact revision, summary, and full command.
  Deployment runs only if the user sends `/approve <request-id>`. Each approval
  is one-use and expires after 15 minutes.
- The approved command executes as root on the `pickbot` VPS, so commands should
  target local production paths directly rather than SSHing back into the same
  server.
- For the receptionist itself, request a detached deployment command because
  restarting the bot would otherwise kill the executor:
  `systemd-run --unit=telegram-receptionist-deploy-$(date +%s) --collect /usr/local/libexec/deploy-telegram-receptionist-worker`
