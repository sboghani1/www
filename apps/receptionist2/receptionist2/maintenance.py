from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import CommonConfig
from .database import Database

RUN_ARTIFACT_SUFFIXES = (".receipt.json", ".stdout", ".stderr")


def backup_database(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"receptionist2-{stamp}.db"
    with sqlite3.connect(database_path) as source:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
    backup_path.chmod(0o600)
    return backup_path


def remove_older_than(paths: list[Path], max_age_days: int, now: float) -> int:
    cutoff = now - max_age_days * 86400
    removed = 0
    for path in paths:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def run_maintenance(
    state_dir: Path,
    *,
    record_days: int = 30,
    backup_days: int = 14,
    run_artifact_days: int = 14,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    database_path = state_dir / "receptionist2.db"
    if not database_path.is_file():
        raise RuntimeError(f"database is missing: {database_path}")
    backup_dir = state_dir / "backups"
    backup = backup_database(database_path, backup_dir)
    backups_removed = remove_older_than(
        list(backup_dir.glob("receptionist2-*.db")), backup_days, now
    )
    run_dir = state_dir / "runs"
    run_files = (
        [
            path
            for path in run_dir.iterdir()
            if path.name.endswith(RUN_ARTIFACT_SUFFIXES)
        ]
        if run_dir.is_dir()
        else []
    )
    database = Database(database_path)
    database.initialize()
    pruned = database.prune(days=record_days)
    return {
        "backup": str(backup),
        "backups_removed": backups_removed,
        "run_artifacts_removed": remove_older_than(
            run_files, run_artifact_days, now
        ),
        "pruned": pruned,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-days", type=int, default=30)
    parser.add_argument("--backup-days", type=int, default=14)
    parser.add_argument("--run-artifact-days", type=int, default=14)
    args = parser.parse_args()
    result = run_maintenance(
        CommonConfig.from_env().state_dir,
        record_days=args.record_days,
        backup_days=args.backup_days,
        run_artifact_days=args.run_artifact_days,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
