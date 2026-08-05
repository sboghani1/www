from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .database import Database


def run_maintenance(
    state_dir: Path,
    *,
    event_days: int = 30,
    backup_days: int = 14,
) -> Path:
    database_path = state_dir / "receptionist.db"
    if not database_path.is_file():
        raise RuntimeError(f"database is missing: {database_path}")
    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"receptionist-{stamp}.db"
    with sqlite3.connect(database_path) as source:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
    backup_path.chmod(0o600)

    cutoff = datetime.now(UTC) - timedelta(days=backup_days)
    for candidate in backup_dir.glob("receptionist-*.db"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()

    Database(database_path).prune_events(event_days)
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/var/lib/telegram-receptionist"),
    )
    parser.add_argument("--event-days", type=int, default=30)
    parser.add_argument("--backup-days", type=int, default=14)
    args = parser.parse_args()
    backup = run_maintenance(
        args.state_dir,
        event_days=args.event_days,
        backup_days=args.backup_days,
    )
    print(backup)


if __name__ == "__main__":
    main()

