---
name: watchdog-diagnose
description: Diagnose and repair a Receptionist2 outage from deterministic evidence.
---

# Receptionist2 watchdog diagnosis

Start from the supplied `receptionist2_status_v1` bundle. Verify evidence before
changing anything.

1. Identify whether the fault is in Telegram polling, the gateway, worker,
   SQLite state, provider authentication, an active run, delivery, scheduling,
   deployment, host resources, or networking.
2. Inspect bounded service logs and exact unit configuration.
3. Preserve queued work, the outbox, provider sessions, and unrelated
   repositories.
4. Never print bot tokens, OAuth tokens, environment-file contents, or other
   credentials.
5. You have operator-approved root authority. Use it only for changes necessary
   to restore Receptionist2, and record every command that changed state.
6. Do not deploy unrelated repository changes.
7. Verify the gateway, worker, database, provider, and pending deliveries after
   remediation.
8. Return a concise incident report: cause, evidence, actions, verification,
   and remaining risk.
