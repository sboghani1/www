---
name: receptionist2-knowledge-transfer
description: Propose reusable learnings from this conversation for www/knowledge_base, then write and push them after the user's feedback.
---

# Receptionist2 knowledge transfer

Run this for `KNOWLEDGE_TRANSFER` prompts, from any chat at any point. The
goal is that a future agent with no memory of this conversation benefits from
what was learned here. It has two turns: propose, then write after the user
answers. Never write before the user has responded to the proposal.

## Turn 1: propose

1. Read `www/knowledge_base/0index.md` (its writing rules are binding) and
   every topic that relates to this conversation.
2. Review the whole conversation, plus `FOCUS` if the prompt gives one.
   Collect candidate learnings: how a system actually works, root causes,
   gotchas and their fixes, verified commands and procedures, constraints,
   and corrections to claims already in the knowledge base.
3. Keep only what is general and reusable. Drop session history, task
   status, one-off values, and anything the rules forbid in a public
   repository (secrets, IPs, account emails, Telegram IDs, exploitable
   details). Check each claim against the evidence; verify cheap uncertain
   ones and mark the rest as uncertain.
4. If nothing qualifies, reply "No reusable learnings to record." and stop.
5. Otherwise reply with the proposal and change no files:
   - For each file, `add` or `edit` and its name, then the learnings it would
     gain (one short bullet each) and, for edits, what would be corrected or
     removed.
   - Your view of topic boundaries: where a topic is getting too broad and
     could be split, or where small topics could be merged, with the
     reasoning.
   - Ask the user to approve, or to say how topics should be broken down more
     or less, before anything is written.

## Turn 2: write (after the user replies)

1. Apply the user's feedback: split, merge, rename, drop, or reword exactly
   as they asked. If the feedback changes the plan substantially, show the
   revised plan and wait again.
2. Merge into existing topics first; refresh `Verified:` dates. List every
   new topic in the index with its description and connections, and update
   the connections of related topics.
3. Commit only the knowledge base, in the `www` checkout under the workspace
   root (`/home/receptionist/repos/www`):

   ```bash
   git -C /home/receptionist/repos/www add knowledge_base
   git -C /home/receptionist/repos/www commit -m "knowledge_base: <summary>" -- knowledge_base
   git -C /home/receptionist/repos/www pull --rebase --autostash
   git -C /home/receptionist/repos/www push
   ```

   Resolve a rebase conflict in `knowledge_base` by keeping both sides'
   content. Never commit other files as part of this.
4. Reply with one line per topic changed and the commit hash.

## From a tombstone

When writing a tombstone, do Turn 1's steps 1-4 but put the proposal in the
tombstone's `## Pending knowledge transfer` section instead of replying with
it. The chat that resumes from the tombstone presents it and continues with
Turn 2 once the user answers.
