# WNBA Poller and Guesser Receptionist Handoff

## Purpose

This file is the durable handoff for the Claude receptionist running on the
`pickbot` VPS. Continue the work described in
`plans/wnba_poller.plan.txt`, guide the user through live testing, and update
this file after every meaningful implementation, validation, deployment, or
troubleshooting step.

Do not rely on the prior chat history. Treat the repository, the main plan, and
this handoff as the source of truth.

## Target outcome

Deliver a continuously running VPS system with:

1. A rolling 14-day ESPN WNBA schedule in the `asce Guesser` Google Sheet.
2. BetOnline full-game moneyline/spread/total plus first-half spread/total.
3. Hourly polling until six hours before tip, then every 15 minutes until tip.
4. A standalone `@wnbaguesser_bot` with a WNBA-only Sheet allowlist initially
   containing only the owner.
5. NFL-style structured thought capture:
   game -> period -> market -> side -> reasoning, with repeatable updates.
6. A receptionist workflow that selects a game and offers:
   - **Generate now**, which sends the standard task directly to Claude.
   - **Copy template**, which lets the user paste, edit, append extra thoughts,
     and send the same task through the normal message path.
7. A deterministic-first Claude Code WNBA lean skill that reads
   `apps/wnba-poller/path210.md`, line history, and Sheet thoughts; generates
   full-game side/total and meaningful first-half leans; updates `path210.md`;
   records an append-only Sheet revision; validates; commits; and pushes.
8. Latest active lean and chronological thought history visible from both bots,
   with separate superseded/deleted revision history.
9. Edit, delete, and undo-latest support through the receptionist.

## Non-negotiable decisions and guardrails

- Read the complete `plans/wnba_poller.plan.txt` before editing.
- Read repository instruction files before changing their areas.
- The standalone WNBA bot captures structured user thoughts. The receptionist
  owns Claude-generated analytical leans.
- Use deterministic Python helpers for game lookup, IDs, normalization,
  extraction, structural validation, file mutation, Sheet revision state, undo,
  and Git preconditions/commit/push. Claude should reason and write the lean,
  not perform fragile text surgery or invent state.
- Sheet lean history is append-only. Edits supersede prior revisions; deletes
  create deleted revisions. Never erase history in place.
- Every accepted `path210.md` generation/edit/delete/undo gets one auditable
  commit pushed to `www/main`.
- Production service deployment still requires explicit Telegram approval.
  Git commits and pushes are not deployments.
- Do not reveal or commit the Telegram token, Odds API key, Google credentials,
  or user IDs.
- Preserve the bot/agent Unix identity and credential boundaries.
- Keep one active agent run globally because the shared VPS has roughly 1GB RAM.
  Treat kernel OOM kills or sustained swap exhaustion as a resize signal.
- Do not modify or clean the original dirty source worktree.

## Important paths

- Monorepo: `/home/receptionist/repos/www`
- Main plan: `plans/wnba_poller.plan.txt`
- This handoff: `plans/wnba_poller.receptionist_handoff.md`
- WNBA app: `apps/wnba-poller`
- Active decision log: `apps/wnba-poller/path210.md`
- Untouched migration evidence: `apps/wnba-poller/source_artifacts`
- Receptionist app: `apps/telegram-receptionist`
- Original Mac source path, recorded for provenance only:
  `/Users/boghani/repos/line-movement-wt-2026-07-02-path-to-10-1783060088855`
- Spreadsheet ID:
  `1toTFz0zmeMQI5WnuWr-jhg0HwVkMPEF5nLBIe0R3wyk`
- Spreadsheet title: `asce Guesser`
- New bot username: `@wnbaguesser_bot`
- VPS: `pickbot` / `209.38.51.86`

## Existing evidence and completed preparation

- The copied Google Sheet is shared with the existing service account.
- A pre-migration workbook backup was created outside Git before any mutation:
  `~/.copilot/session-state/.../files/wnba-poller/asce-guesser-before-wnba.json`
  on the originating Mac session. Do not claim it exists on the VPS unless it
  is explicitly copied there.
- Live ESPN WNBA date/range fetching was validated.
- Live BetOnline WNBA event odds returned:
  `h2h`, `spreads`, `totals`, `spreads_h1`, and `totals_h1`.
- No active Mac process, launch agent, cron job, or Copilot schedule was found
  polling WNBA at investigation time.
- The original private Mac Odds API key file is intentionally retained as an
  emergency fallback, but the Mac must not run a scheduler after cutover.
- The new BotFather token was validated for `@wnbaguesser_bot`.
- The Sheet must not be replaced until its backup and initialization command
  are verified.

## Resume procedure

1. Pull `www/main` and confirm the exact revision.
2. Read:
   - `CLAUDE.md`
   - `plans/wnba_poller.plan.txt`
   - this handoff
   - relevant app instruction files
3. Inspect `git status` and the latest commit before editing.
4. Review the current partial implementation and compare it to the plan.
5. Run the smallest existing Python 3.12 test suites for both apps.
6. Fix or complete local implementation before touching the live Sheet or VPS
   services.
7. Update **Current progress** below with findings and the exact next action.

## Remaining implementation checklist

- [ ] Confirm the standalone WNBA poller package and artifact checksums.
- [ ] Confirm ESPN rolling-14-day sync and manual-column preservation.
- [ ] Confirm full-game bulk plus event-specific first-half polling.
- [ ] Confirm hourly/>6h and 15-minute/<=6h due calculations.
- [ ] Confirm Sheet schemas, initialization backup flag, and history import.
- [ ] Finish and test standalone WNBA Guesser bot.
- [ ] Finish Sheet-backed WNBA-only allowlist seeding.
- [ ] Finish receptionist game picker and Generate-now/Copy-template actions.
- [ ] Ensure copied/edited templates route to the same immutable game task.
- [ ] Implement deterministic WNBA lean helper scripts.
- [ ] Implement `.claude/skills/wnba-lean/SKILL.md`.
- [ ] Implement append-only lean revision states.
- [ ] Implement deterministic `path210.md` append/edit/delete/undo.
- [ ] Implement validation and one-change Git commit/push.
- [ ] Implement shared active/history views in both bots.
- [ ] Run complete local Python 3.12 tests and syntax checks.
- [ ] Commit and push coherent implementation.
- [ ] Verify/install VPS secret environment without printing values.
- [ ] Initialize the Sheet only after asking the user for confirmation.
- [ ] Install/enable schedule, poller, and WNBA bot systemd units.
- [ ] Deploy receptionist changes only through the approval broker.
- [ ] Run the guided production acceptance sequence.

## Guided user test sequence

Ask for the user only when ready for these steps:

1. Confirm replacing the copied NFL Sheet tabs with WNBA tabs.
2. Confirm/approve production deployment.
3. Ask the user to send `/start` to `@wnbaguesser_bot`.
4. Verify only the owner is authorized.
5. Select a game and submit a structured thought.
6. Submit a second updated thought for the same game.
7. View the thought history in the WNBA bot.
8. In the receptionist, select the same game and tap **Generate now**.
9. Verify the lean reads `path210.md`, current lines, and both thoughts.
10. Verify full-game side/total and conditional first-half output.
11. Verify automatic `path210.md` append, Sheet revision, commit, and push.
12. Use **Copy template**, append extra thoughts, and send it.
13. Verify it follows the identical generation path.
14. View the active lean and history from both bots.
15. Ask the receptionist to edit the lean.
16. Ask it to delete the lean, inspect deleted history, then undo.
17. Restart services and confirm selection/history/state persistence.
18. Confirm timers, quota reporting, memory, and no duplicate polling.

## Troubleshooting rules

- Record the timestamp, failing command/action, concise error, root cause, fix,
  and validation under **Troubleshooting log**.
- Never paste secret-bearing URLs, environment values, or credential JSON.
- Do not silently recreate or clear Sheet tabs after a failure.
- Do not replay interrupted thoughts, Claude generations, Sheet revisions, or
  Git mutations without checking idempotency state.
- On a stale Git revision or dirty worktree, stop and resolve explicitly rather
  than force-resetting user work.
- On OOM evidence, preserve logs and recommend a larger VPS.

## Current progress

Updated 2026-08-05:

- Product architecture and acceptance flow are fully documented in the main
  plan.
- Core poller and standalone bot implementation were started locally.
- Standalone `@wnbaguesser_bot` flow is implemented locally.
- Partial cross-app work exists for append-only lean revisions, deterministic
  context/path210/workflow helpers, the Claude skill, receptionist game picker,
  template routing, history, undo, durable selection state, shared Guesser
  history, and the fixed privileged WNBA helper.
- Python 3.12 syntax passed for 27 files, deployment-worker shell syntax passed,
  and `git diff --check` passed. The earlier standalone suite had 35 passing
  tests, but the complete suites have not run after the cross-app changes.
- No live Sheet replacement or WNBA service deployment had occurred at this
  checkpoint.
- Before continuing, inspect the committed checkpoint and run tests; do not
  assume every checklist item is complete merely because files exist.

## Troubleshooting log

### 2026-08-05 — Git publication sequencing is incomplete

- Symptom: the current `GitPublisher.publish()` implementation checks for a
  clean repository after `path210.md` has already been written, so a normal lean
  publication fails its own precondition.
- Next fix: validate unrelated cleanliness and expected base revision before
  writing; permit only the deterministic expected `path210.md` change afterward;
  then validate, commit exactly that change, and push with explicit failure
  recovery.
- Related follow-up: update receptionist test `Config` constructors for the new
  WNBA fields and review Sheet publication-receipt failure/recovery.
- Required test command after the fix:

  ```bash
  cd /home/receptionist/repos/www
  PYENV_VERSION=3.12.11 python -m pytest \
    apps/wnba-poller/tests apps/telegram-receptionist/tests \
    -q -p no:cacheprovider
  ```
