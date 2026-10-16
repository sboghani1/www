---
name: receptionist2-recurring-task
description: Execute one durable occurrence of a recurring Receptionist2 task.
---

# Recurring task execution

Treat this invocation as one scheduled occurrence, not an open-ended loop.

1. Read the startup context manifest when present.
2. Determine the exact scheduled task and reporting period from the prompt.
3. Query authoritative local state before relying on earlier conversational
   conclusions.
4. Produce a self-contained update suitable for delivery without follow-up.
5. State unavailable or stale inputs explicitly; never turn missing data into
   a success-shaped report.
6. Do not create another scheduler, sleep loop, cron entry, or background
   process. Receptionist2 owns future occurrences.
