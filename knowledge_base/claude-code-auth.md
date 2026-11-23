# Claude Code authentication for headless services

Verified: 2026-11-23 (Claude Code 2.1.302 on the VPS; Receptionist2 `/reauth`
outage and fix; Bell installer and NBA intake).

## Two credential kinds

- **Interactive login** (`~/.claude/.credentials.json`): access token plus a
  refresh token. The refresh token has a hard expiry
  (`refreshTokenExpiresAt`); once past it, Claude Code blanks the stored
  tokens and every call fails with "authentication expired". Silent until it
  breaks.
- **`claude setup-token`**: a one-year OAuth token for non-interactive use,
  supplied as `CLAUDE_CODE_OAUTH_TOKEN`. Preferred for services. Receptionist2
  keeps it in `/home/receptionist-agent/.claude/auth.env` (mode 0600), loaded
  by the agent runner and by `decision-helper-bot.service`
  (`EnvironmentFile=`); services read it only at start, so restart them after
  renewing.

## Everything that calls Claude must load `auth.env`

Once the interactive login has expired, only `CLAUDE_CODE_OAUTH_TOKEN` from
`auth.env` works. Units get it through `EnvironmentFile=`, but installers and
hand-run commands do not: Bell's installer check (`claude auth status`)
reported "not logged in" while the service worked, and a hand-run
`nba-channel-intake` failed with "OAuth session expired". Source the file in
the target account's shell (`set -a; . ~/.claude/auth.env; set +a`) so the
token travels in the environment, never argv; Bell's installer does this in
its `as_agent` wrapper.

## `setup-token` quirks

- It only renders on a TTY: run it in a PTY when automating. Output uses CSI
  cursor moves (`ESC[nG`) instead of spaces, and the authorize URL is inside
  an OSC 8 hyperlink, so strip escapes before parsing.
- The token's scope is `user:inference` only. `/api/oauth/profile` returns
  403, so you cannot ask it which account it belongs to.
- To check the account, send a minimal `/v1/messages` request with the token
  and compare its `anthropic-organization-id` response header with
  `oauthAccount.organizationUuid` in the intended user's `~/.claude.json`.
- Sign in to the intended Claude account in the browser before authorizing;
  the flow uses whichever account the browser has.

## Precedence and hygiene

- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and the
  Bedrock/Vertex switches override subscription auth; remove them from the
  environment of services that must use the subscription.
- Pass tokens by environment (`sudo --preserve-env=CLAUDE_CODE_OAUTH_TOKEN`),
  never in argv, where `ps` shows them.
- `claude auth status` exits 1 when logged out but still prints useful JSON;
  parse it instead of discarding output on non-zero exit.
- When rewriting an env file: back it up, write a temp file with the same
  owner and mode, replace only the target line (last assignment wins), and
  `mv` it into place atomically.
