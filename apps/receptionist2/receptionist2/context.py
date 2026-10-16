from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import Database

BOOTSTRAP_SKILL = (
    "www/apps/receptionist2/skills/receptionist2-bootstrap/SKILL.md"
)


def build_context_manifest(
    database: Database,
    *,
    user_id: int,
    repo_root: Path,
    timezone: str,
) -> dict[str, Any]:
    session = database.active_session(user_id)
    schedules = database.list_schedules(user_id)
    queue = database.queue_summary()
    workspace_loaded = repo_root.is_dir()
    repositories = (
        sorted(
            path.name
            for path in repo_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        if workspace_loaded
        else []
    )
    return {
        "schema": "receptionist2_context_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "timezone": timezone,
        "workspace": str(repo_root),
        "repositories": repositories,
        "active_session": {
            "id": session["id"],
            "name": session["display_name"],
            "model": session["model"],
            "effort": session["effort"],
            "has_provider_context": bool(session["provider_session_id"]),
        },
        "queue": queue,
        "schedules": [
            {
                "id": row["id"],
                "name": row["name"],
                "cron": row["cron"],
                "timezone": row["timezone"],
                "enabled": bool(row["enabled"]),
                "next_run_at": row["next_run_at"],
            }
            for row in schedules
        ],
        "sources": {
            "database": {"status": "loaded"},
            "workspace": {
                "status": "loaded" if workspace_loaded else "missing"
            },
            "schedules": {"status": "loaded", "rows": len(schedules)},
        },
    }


def bootstrap_prompt(prompt: str, manifest: dict[str, Any]) -> str:
    return (
        f"Read and follow `{BOOTSTRAP_SKILL}` before doing the user's work.\n"
        "The deterministic startup context is below. Do not claim readiness "
        "until every required source reports status=loaded. If any required "
        "source is missing, report the gap instead of proceeding.\n\n"
        "RECEPTIONIST2_CONTEXT_V1\n"
        f"{json.dumps(manifest, ensure_ascii=False, sort_keys=True)}\n"
        "END_RECEPTIONIST2_CONTEXT_V1\n\n"
        "After validating the context, perform this exact user request:\n"
        f"{prompt}"
    )
