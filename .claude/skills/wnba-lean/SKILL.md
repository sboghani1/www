---
name: wnba-lean
description: Generate, revise, delete, or undo WNBA analytical leans using Sheet context and path210 rules.
---

# WNBA lean workflow

Use this skill for a validated `WNBA_LEAN_REQUEST_V1` prompt or a natural-language
request to revise, delete, or undo a WNBA lean.

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
