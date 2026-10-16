---
name: receptionist2-bootstrap
description: Validate Receptionist2 startup context before beginning work.
---

# Receptionist2 startup

Before acting on the user's first request in a provider session:

1. Read the complete `RECEPTIONIST2_CONTEXT_V1` manifest supplied in the prompt.
2. Confirm every required source has `status=loaded`.
3. Understand the workspace root, available repositories, active session,
   configured model and effort, queued work, and recurring schedules.
4. If required context is missing or contradictory, report the exact gap and do
   not claim readiness.
5. Do not repeat the manifest to the user. Once validated, perform the user's
   request normally.

The database and scheduler are authoritative for queued and recurring work.
Do not invent tasks from old transcripts or repository notes.
