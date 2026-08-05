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
- Private inputs are staged on the VPS for the agent, without being committed:
  - `/home/receptionist-agent/.config/wnba-poller/telegram-token`
  - `/home/receptionist-agent/.config/wnba-poller/odds.env`
  Both are owned by `receptionist-agent` with mode `0600`. Build the final
  service environment from these files without printing their values.
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

- [x] Confirm the standalone WNBA poller package and artifact checksums.
      (`test_source_artifacts.py` passes.)
- [x] Confirm ESPN rolling-14-day sync and manual-column preservation.
      (`test_espn.py` passes.)
- [x] Confirm full-game bulk plus event-specific first-half polling.
      (`test_odds.py` passes.)
- [x] Confirm hourly/>6h and 15-minute/<=6h due calculations.
      (`test_scheduler_service.py` passes.)
- [x] Confirm Sheet schemas and initialization backup flag.
      (`test_sheets.py` passes.) History import remains intentionally
      unimplemented — see plan's "Implementation boundaries".
- [x] Finish and test standalone WNBA Guesser bot.
      (`test_guesser_bot.py`, 11 tests, incl. fixed `hist`-button assertion.)
- [x] Finish Sheet-backed WNBA-only allowlist seeding. (covered in
      `test_sheets.py`/`test_guesser_bot.py`.)
- [ ] Finish receptionist game picker and Generate-now/Copy-template actions.
      Code exists in `receptionist/wnba.py` and `receptionist_helper.py`; not
      yet re-verified end-to-end this session (see Current progress next
      actions).
- [ ] Ensure copied/edited templates route to the same immutable game task.
      Same verification gap as above.
- [x] Implement deterministic WNBA lean helper scripts. (`lean_context.py`,
      `path210_ops.py`, `lean_revisions.py`, `lean_workflow.py` — now covered
      by `test_lean_workflow.py`.)
- [ ] Implement `.claude/skills/wnba-lean/SKILL.md`. File exists; content not
      yet re-checked against the confirmed-correct `lean_workflow.py`
      contract this session.
- [x] Implement append-only lean revision states. (`lean_revisions.py`,
      tested via `derive_revision_history` assertions in
      `test_lean_workflow.py`.)
- [x] Implement deterministic `path210.md` append/edit/delete/undo.
      (`path210_ops.py` + full lifecycle test in `test_lean_workflow.py`.)
- [x] Implement validation and one-change Git commit/push. **GitPublisher
      sequencing bug fixed this session** — see Troubleshooting log.
- [ ] Implement shared active/history views in both bots. Guesser side
      (`hist`/`revs` callbacks) is implemented and tested; receptionist side
      not yet re-verified this session.
- [x] Run complete local Python 3.12 tests and syntax checks. 73 passed,
      `py_compile` clean, `bash -n` clean on the installer, `git diff --check`
      clean.
- [ ] Commit and push coherent implementation. (About to do for this chunk's
      fixes; cross-app verification above still outstanding before final
      push.)
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

Updated 2026-08-05 (resumed from checkpoint `622b729`):

- Product architecture and acceptance flow are fully documented in the main
  plan.
- Core poller and standalone bot implementation were started locally.
- Standalone `@wnbaguesser_bot` flow is implemented locally.
- Partial cross-app work exists for append-only lean revisions, deterministic
  context/path210/workflow helpers, the Claude skill, receptionist game picker,
  template routing, history, undo, durable selection state, shared Guesser
  history, and the fixed privileged WNBA helper.
- **GitPublisher sequencing bug (see Troubleshooting log) is fixed and
  covered by new tests.** `apps/wnba-poller/tests/test_lean_workflow.py` is
  new: 24 tests covering `GitPublisher` preconditions (clean-before-write,
  scoped-clean-after-write, wrong branch, stale base, unrelated dirty files,
  path scope), the full `execute_revision` create/revise/delete/undo
  lifecycle against a real local git repo + bare "origin", failure injection
  on `git commit`/`git push` (confirms abort receipts, prior active lean
  preserved, working tree restored, repo left matching `origin/main`),
  `WNBA_LEAN_REQUEST_V1` template build/parse round-trip and Generate-now
  equivalence, and `validate_lean_output` schema enforcement (including the
  non-empty-watch-conditions requirement, which is intentional per plan
  section 8A output defaults).
- Fixed a stale assertion in `test_guesser_bot.py`
  (`test_exact_reasoning_is_appended_and_repeat_button_is_offered`): the
  confirmation keyboard now includes the plan-required `View game history`
  (`hist`) button; the test only expected the older two-button list.
- Fixed `apps/telegram-receptionist/tests/test_deployment_requests.py`: its
  direct `Config(...)` construction predated the `wnba_helper` /
  `wnba_helper_timeout_seconds` fields and failed with a `TypeError`. Added
  both fields to the test's constructor call.
- Both apps' `pyproject.toml` now set `addopts = "-q --import-mode=importlib"`
  so the combined test command below works — `tests/test_config.py` exists in
  both apps and pytest's default (`prepend`) import mode raises an
  `import file mismatch` collection error when both are collected in one
  session without unique package names. (Adding `tests/__init__.py` to both
  was tried first and made it worse — both packages then import as the same
  top-level `tests` name and collide harder. `--import-mode=importlib` is the
  correct fix and needs no `__init__.py`.)
- Full combined suite is green: `73 passed` (37 wnba-poller incl. the 24 new
  lean_workflow tests, plus telegram-receptionist's suite; see the exact
  command in Troubleshooting log). `py_compile` passed for every
  `wnba_poller` and `receptionist` module, `install-wnba-poller` passed
  `bash -n`, and `git diff --check` is clean.
- No live Sheet replacement or WNBA service deployment had occurred at this
  checkpoint.
- VPS readiness was verified at commit `b39a909`: `main` matched `origin/main`,
  the workspace was clean, the plan/skill/app files were present, Google Sheet
  access and Claude authentication worked, and the existing receptionist
  service and health check were active. Not re-verified yet this session.

Next actions (in order):

1. Add focused unit tests for `lean_context.py`, `path210_ops.py`, and
   `lean_revisions.py` in isolation (currently only exercised indirectly
   through `test_lean_workflow.py`'s integration-style tests).
2. Verify `apps/telegram-receptionist/receptionist/wnba.py` and
   `deploy/receptionist-wnba-helper` end-to-end against the plan's section 7
   receptionist flow (game picker, Generate now / Copy template, template
   routing/revalidation, shared history views, edit/delete/undo).
3. Re-verify `.claude/skills/wnba-lean/SKILL.md` content matches the
   deterministic-first contract in plan section 8A now that
   `lean_workflow.py` behavior is confirmed correct.
4. Dry-run the deterministic fixture flow from plan section 13 step 2 before
   touching any Sheet.

## Troubleshooting log

### 2026-08-05 — Git publication sequencing is incomplete

- Symptom: the current `GitPublisher.publish()` implementation checks for a
  clean repository after `path210.md` has already been written, so a normal lean
  publication fails its own precondition.
- Root cause confirmed: `execute_revision()` calls `publisher.precondition()`
  once, correctly, before any write (establishing `base_sha`). But
  `GitPublisher.publish()` — called *after* `path210_path.write_text(after,
  ...)` — called `self.precondition(...)` again internally, which re-runs the
  full `git status --porcelain` "must be empty" check. That check now always
  fails, because path210.md was intentionally dirtied moments earlier.
- Fix applied: split `precondition()` (used once, pre-write, requires a fully
  clean tree) from a new `_publish_precondition()` (used inside `publish()`,
  post-write) that checks branch/HEAD/origin the same way but scopes the
  "clean" check to exclude exactly the one expected `path210.md` line from
  `git status --porcelain`, via a new `_status_lines()` helper.
- Second bug found while testing the fix: `_status_lines()` must **not**
  reuse `GitPublisher._git()`, because `_git()` does `.stdout.strip()` on the
  *whole* multi-line blob. Porcelain output is fixed-column
  (`XY<space>path`), and stripping the whole blob eats the leading space of
  the first line whenever its index-status column is blank (the common case
  for a plain worktree edit), shifting every `line[3:]` slice off by one and
  making every legitimate publish fail with "unrelated working tree changes
  present". Fixed by reading raw `stdout` and only `.rstrip("\n")`-ing before
  splitting into lines, never a full `.strip()`.
- Also tried and reverted: adding `tests/__init__.py` to both apps' test
  directories to fix the `test_config.py` collection collision (see the
  "combined suite" entry below) — this made collection *worse*, since both
  packages then import as the literal same name `tests` and collide harder.
  `--import-mode=importlib` in each `pyproject.toml`'s `addopts` was the
  actual fix and needed no `__init__.py`.
- Validation: `apps/wnba-poller/tests/test_lean_workflow.py` (new, 24 tests)
  exercises the fixed `GitPublisher` against a real local git repo with a
  bare "origin" remote — including the exact previously-broken path (publish
  succeeding when only path210.md is dirty), unrelated-dirty-file rejection,
  stale-base rejection, wrong-branch rejection, and full
  create/revise/delete/undo through `execute_revision` with injected
  `git commit` and `git push` failures confirming abort receipts and that the
  prior active lean survives.
- Required test command (now passes, `73 passed`):

  ```bash
  cd /home/receptionist/repos/www
  PYENV_VERSION=3.12.11 python -m pytest \
    apps/wnba-poller/tests apps/telegram-receptionist/tests \
    -q -p no:cacheprovider
  ```

### 2026-08-05 — Combined test command fails to collect without importlib mode

- Symptom: running both apps' `tests/` directories in one pytest session
  raised `import file mismatch` for `test_config.py`, which exists (as an
  unrelated file, different content) in both `apps/wnba-poller/tests/` and
  `apps/telegram-receptionist/tests/`.
- Root cause: pytest's default `prepend` import mode requires globally-unique
  top-level module names when there is no `tests/__init__.py`; two modules
  named `test_config` from different directories collide in `sys.modules`.
- Fix: set `addopts = "-q --import-mode=importlib"` in both apps'
  `pyproject.toml`. This mode imports each test file by its own file path
  without requiring unique module basenames, so no `__init__.py` is needed.
- Validation: the exact command in the entry above now collects and passes
  all 73 tests with zero collection errors.
