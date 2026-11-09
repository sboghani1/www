---
name: receptionist2-tombstone
description: Write a self-contained handoff file so a fresh chat can continue this conversation's work, or resume work from such a file.
---

# Receptionist2 tombstone

A tombstone lets a new chat with a fresh context window continue this chat's
work as if nothing had changed. The next agent reads only the tombstone, so
anything not written in it is lost.

## Writing (`TOMBSTONE_PATH=...` with "Write the tombstone")

First prepare a knowledge-transfer proposal as described under "From a
tombstone" in
`www/apps/receptionist2/skills/receptionist2-knowledge-transfer/SKILL.md`, so
the reusable lessons of this chat outlive it. Do not edit the knowledge base
now; the user gives feedback on the proposal in the next chat.

Then write one Markdown file at exactly `TOMBSTONE_PATH`, creating nothing
else.
Reconstruct the whole conversation, not just the latest turn. Be specific:
exact paths, branches, commit hashes, unit names, IDs, URLs, commands, and
numbers. Never include secrets or tokens; say where they live instead.

Use these sections, in order, and write "None." for an empty one:

1. `# <chat name> — tombstone` with the date and time written.
2. `## Goal` — what the user is ultimately trying to achieve and why.
3. `## User requirements and preferences` — every requirement, constraint,
   and preference the user stated, quoted exactly where possible, including
   corrections they made to your work.
4. `## Decisions` — each decision with its reason and the alternatives
   rejected.
5. `## Done` — what was completed: files changed, commits pushed, deploys,
   jobs started, messages sent, with how each was verified.
6. `## Current state` — what is true right now: running jobs, uncommitted
   changes, open branches, data written, services' health.
7. `## Open problems` — known bugs, failures, risks, and unverified
   assumptions, with the evidence for each.
8. `## Next steps` — the planned work in order, each concrete enough to start
   without asking.
9. `## Pending questions` — anything waiting on the user.
10. `## Key references` — files, docs, commands, and links the next agent
    should read first, including the knowledge-base topics that apply.
11. `## Pending knowledge transfer` — the knowledge-transfer proposal (files
    to add or edit, the learnings for each, and topic-boundary suggestions),
    or "None."

Then reply in two or three lines: the path and a one-line summary. Do not
start any of the next steps.

## Resuming (`TOMBSTONE_PATH=...` with "Continue from the tombstone")

Read the whole file at `TOMBSTONE_PATH` and treat it as the history of this
conversation: its requirements and preferences bind you as if the user had
just said them. Check that anything it calls current (branches, running
jobs, files) still holds before relying on it. Reply briefly with
`Picked up:` (the goal and current state in one or two lines), `Next:` (the
first next steps), and any pending questions. If the tombstone has a pending
knowledge transfer, present that proposal and ask for feedback on it (Turn 2
of the knowledge-transfer skill follows the user's answer). Do not start the
next steps until the user replies.
