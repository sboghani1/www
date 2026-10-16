from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from .config import DEFAULT_EFFORT, DEFAULT_MODEL, CommonConfig
from .control import status
from .database import Database
from .investigation import investigation_prompt

DIAGNOSTIC_SKILL = Path(
    "www/apps/receptionist2/skills/watchdog-diagnose/SKILL.md"
)


def diagnose(
    database: Database,
    repo_root: Path,
    incident_id: str,
    problem: str | None = None,
) -> str:
    bundle = status(database)
    evidence = (
        "The deterministic Receptionist2 status bundle is:\n\n"
        f"{json.dumps(bundle, ensure_ascii=False, indent=2)}\n\n"
        "You have explicit operator authorization to make necessary "
        "repository, service, and root-level repairs. Record every action and "
        "verify the final state. Do not expose credentials or secret values."
    )
    if problem:
        prompt = (
            f"{investigation_prompt(problem)}\n\n"
            f"Watchdog incident {incident_id}. {evidence}"
        )
    else:
        prompt = (
            f"Read and follow `{DIAGNOSTIC_SKILL}`. Diagnose incident "
            f"{incident_id}. Investigate why Receptionist2 is unhealthy. "
            f"{evidence}"
        )
    result = subprocess.run(
        [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            "/usr/bin/claude",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
            "--model",
            os.getenv("RECEPTIONIST2_DIAGNOSTIC_MODEL", DEFAULT_MODEL),
            "--effort",
            os.getenv("RECEPTIONIST2_DIAGNOSTIC_EFFORT", DEFAULT_EFFORT),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip()[-2000:]
            or f"Diagnostic agent exited with {result.returncode}"
        )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("incident_id")
    args = parser.parse_args()
    config = CommonConfig.from_env()
    database = Database(config.database_path)
    database.initialize()
    incident = database.get_incident(args.incident_id)
    try:
        report = diagnose(
            database,
            config.repo_root,
            str(incident["id"]),
            incident.get("problem"),
        )
    except Exception as error:
        database.finish_incident(args.incident_id, error=str(error))
        raise
    database.finish_incident(args.incident_id, report=report)
    print(report)


if __name__ == "__main__":
    main()
