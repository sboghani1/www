---
name: receptionist2-investigate
description: Investigate an operator-reported problem and return a structured, verified report.
---

# Receptionist2 investigation

Use this workflow for `INVESTIGATION_V1` prompts.

1. Identify the exact failing surface: Telegram bot, service, timer, job,
   repository, or data store. Search for displayed error text across all
   applications because several bots share wording.
2. Map the surface to its systemd unit and inspect bounded journal output
   (`journalctl -u <unit> -n 200 --no-pager`).
3. Read the relevant code before theorizing. Compare the running release with
   the repository revision.
4. Reproduce with real inputs. Never print credentials or environment files.
5. Fix the root cause. Commit and push clean, tested changes, then queue any
   production deployment through `request-receptionist-deploy`.
6. Prove the fix through the real entry point, not only unit tests.
7. Report with these headings, in order: `## Findings`, `## Evidence`,
   `## Changes`, `## Validation`, `## Lessons`.
8. Add a lesson below only when it is generalizable to unrelated future
   investigations. Commit lesson edits with the fix. Do not add incident-only
   notes.

## Lessons

1. The receptionist service and agent namespaces see `/` read-only. Do not
   infer a host filesystem incident from a write test there; run
   `sudo -n /usr/local/libexec/receptionist-host-recovery diagnose`.
