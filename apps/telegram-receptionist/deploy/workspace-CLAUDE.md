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
- The only production deployment command currently authorized is:
  `sudo -n /usr/local/sbin/deploy-telegram-receptionist`.

