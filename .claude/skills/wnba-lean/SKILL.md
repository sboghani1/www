---
name: wnba-lean
description: Generate, revise, delete, undo, or resolve WNBA analytical leans using Sheet context and path210 rules.
---

# WNBA lean workflow

Use this skill for a validated `WNBA_LEAN_REQUEST_V1` prompt, a natural-language
request to revise, delete, or undo a WNBA lean, or a receptionist-queued request
to resolve a lean (grade it against the game's final score).

## Safety and ownership

- Run from the `www` repository root.
- Treat the immutable `event_id` as authoritative, but always validate the
  supplied matchup against current Sheet state.
- Never print environment variables, credentials, tokens, or raw configuration.
- Never edit `path210.md`, Sheet revisions, or Git state manually.
- Do not invoke arbitrary scripts, paths, modules, or repositories.
- Python owns lookup, context extraction, validation, path210 surgery, append-only
  Sheet revision writes, Git preconditions, the one commit, push, and recovery.
- Claude owns only the analytical choice and wording of the structured output.

## 1. Load deterministic context

Send this JSON object to the fixed
`/opt/telegram-receptionist/current/.venv/bin/wnba-lean-workflow` executable on
stdin:

```json
{
  "action": "context",
  "event_id": "<immutable event ID>",
  "matchup": "<away> @ <home>"
}
```

Read all returned snapshots, all user thoughts, the published revision history,
the latest active lean, the path210 rules, model cache, selected-game section,
and any current deterministic event block. Do not invent missing lines or state.

## 2. Choose the operation

- New game with no active published lean: `create`.
- Generate again or update an active lean: `revise`.
- Natural-language removal request: `delete`.
- Undo-latest request: `undo`.
- Receptionist-queued request to grade a published lean against the final
  score: `resolve` (see [Resolve a published lean](#4-resolve-a-published-lean)
  below instead of the create/revise/delete/undo flow).
- For a targeted restore, include `target_revision_id`; otherwise deterministic
  undo restores the prior published non-delete revision.

For create/revise, produce the JSON output below. For delete/undo, send `null`
as output; undo reconstructs the prior published output deterministically.

Claude lean defaults:

- Always provide a full-game side and full-game total.
- Add first-half side and/or total only when evidence supports a meaningful lean.
- Strength is one of `strong`, `moderate`, `small`, or `watch`.
- Cite concrete Sheet/path210 evidence and explicit watch conditions.
- `source_snapshot_ids` must contain only identifiers returned by context.

```json
{
  "full_game": {
    "side": {
      "selection": "<exact away or home team>",
      "strength": "moderate",
      "evidence": ["..."],
      "watch_conditions": ["..."]
    },
    "total": {
      "selection": "Over",
      "strength": "small",
      "evidence": ["..."],
      "watch_conditions": ["..."]
    }
  },
  "first_half": {
    "side": {
      "selection": "<exact away or home team>",
      "strength": "small",
      "evidence": ["..."],
      "watch_conditions": ["..."]
    }
  },
  "summary": "...",
  "source_snapshot_ids": ["<ID returned by context>"]
}
```

## 3. Apply atomically

Send one JSON object to the same fixed
`/opt/telegram-receptionist/current/.venv/bin/wnba-lean-workflow` executable on
stdin:

```json
{
  "action": "apply",
  "event_id": "<immutable event ID>",
  "matchup": "<away> @ <home>",
  "operation": "create",
  "request_text": "<the complete user request>",
  "output": {}
}
```

The helper acquires the workflow lock, revalidates Sheet/game state, validates
the output, appends an unpublished revision, modifies only the event block in
`apps/wnba-poller/path210.md`, validates the change, creates and pushes exactly
one auditable commit to `www/main`, and then appends a publication receipt.
Failures append an abort receipt, restore local content/Git state where safe,
and leave the previous published active revision unchanged.

Report success only after the helper returns `ok: true` with a revision and
commit SHA. On failure, report the visible error without claiming publication.

## 4. Resolve a published lean

The receptionist queues this flow when the user taps "Confirm resolve" on a
game in `/wnba_resolve`. The queued prompt names the immutable `event_id` and
matchup and instructs you to use this skill's resolve workflow — do not treat
it as a create/revise request.

First load context exactly as in step 1, but set `allow_started: true` so the
normal "no longer current" guard (which exists to stop fresh generation on a
stale game) does not block loading a game that has already been played:

```json
{
  "action": "context",
  "event_id": "<immutable event ID>",
  "matchup": "<away> @ <home>",
  "allow_started": true
}
```

Confirm the game has a recorded final score and closing lines in the returned
Sheet state, and confirm the active revision has not already been resolved.
If the score is not yet final, stop and report that resolution cannot proceed
until a final score is recorded — do not guess or wait.

Then send one `resolve` request to the same
`/opt/telegram-receptionist/current/.venv/bin/wnba-lean-workflow` executable:

```json
{
  "action": "resolve",
  "event_id": "<immutable event ID>",
  "matchup": "<away> @ <home>",
  "entry_slug": "<short lowercase path210 entry slug, e.g. fadesparks>",
  "tags": "<path210 tag(s) for this entry>",
  "line_movement": "<optional line-movement note>",
  "context_text": "context: <your narrative context, must start with 'context:'>",
  "model_lean_text": "<optional one-line recap of the original lean, e.g. side (...) -- HIT; total (...) -- HIT>",
  "request_text": "<the complete resolution request>"
}
```

You author only the narrative fields above (`entry_slug`, `tags`,
`line_movement`, `context_text`, `model_lean_text`). The helper always computes
the graded outcome itself from the Sheet's recorded final score and closing
lines, and always assigns the Past Events entry number itself — never accept
or infer a result or entry number and pass it in; the helper ignores anything
you send for those and derives them independently, so do not describe a result
in `context_text`/`model_lean_text` until you have seen the helper's actual
returned `result`.

The helper acquires the workflow lock, re-validates Sheet/game state, grades
the active lean deterministically, converts the matching `WNBA_LEAN_EVENT`
block in `path210.md` into a numbered Past Events entry, deterministically
rebuilds the `# Model Cache` tag counts so they stay in sync with the new entry
(path210 rule #2), validates that no unrelated content changed, creates and
pushes exactly one auditable commit to `www/main`, and appends a publication
receipt marking the revision resolved. Failures append an abort receipt and
restore prior content/Git state. (The Model Cache's interpretive
including/excluding breakouts are refreshed separately during the analytical
lesson pass, not by the deterministic resolve step.)

Report success only after the helper returns `ok: true` with a revision,
commit SHA, and `result`. State the graded result plainly (right/wrong/push)
using the helper's own value — never a value you inferred yourself.
