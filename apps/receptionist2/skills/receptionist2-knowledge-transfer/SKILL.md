---
name: receptionist2-knowledge-transfer
description: Distill reusable understanding from this conversation into www/knowledge_base and push it.
---

# Receptionist2 knowledge transfer

Run this for `KNOWLEDGE_TRANSFER` prompts, from any chat at any point, and as
the first step of writing a tombstone. The goal is that a future agent with
no memory of this conversation benefits from what was learned here.

1. Read `www/knowledge_base/0index.md` (its writing rules are binding) and
   every topic that relates to this conversation.
2. Review the whole conversation, plus `FOCUS` if the prompt gives one.
   Collect candidate learnings: how a system actually works, root causes,
   gotchas and their fixes, verified commands and procedures, constraints,
   and corrections to claims already in the knowledge base.
3. Keep only what is general and reusable. Drop session history, task
   status, one-off values, and anything the rules forbid in a public
   repository (secrets, IPs, account emails, Telegram IDs, exploitable
   details). Check each remaining claim against the evidence you have; verify
   cheap uncertain ones, and mark the rest as uncertain.
4. Merge into existing topics first: correct wrong statements, extend
   sections, refresh `Verified:` dates. Create a topic only for a separate
   subject, and add it with its connections to the index. Keep edits
   concise.
5. If nothing qualifies, change nothing and say so.
6. Commit only the knowledge base, in the `www` checkout under the workspace
   root (`/home/receptionist/repos/www`):

   ```bash
   git -C /home/receptionist/repos/www add knowledge_base
   git -C /home/receptionist/repos/www commit -m "knowledge_base: <summary>" -- knowledge_base
   git -C /home/receptionist/repos/www pull --rebase --autostash
   git -C /home/receptionist/repos/www push
   ```

   Resolve a rebase conflict in `knowledge_base` by keeping both sides'
   content. Never commit other files as part of this.
7. Reply with one line per topic changed (what was learned) and the commit
   hash, or "No reusable learnings to record."
