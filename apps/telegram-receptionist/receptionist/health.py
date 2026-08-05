from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def check(state_dir: Path, maximum_age: int = 45) -> None:
    heartbeat = state_dir / "heartbeat"
    database = state_dir / "receptionist.db"
    if not heartbeat.is_file():
        raise RuntimeError("heartbeat file is missing")
    age = time.time() - heartbeat.stat().st_mtime
    if age > maximum_age:
        raise RuntimeError(f"heartbeat is stale ({age:.0f}s)")
    with sqlite3.connect(database) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("database quick_check failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/var/lib/telegram-receptionist"),
    )
    parser.add_argument("--maximum-age", type=int, default=45)
    args = parser.parse_args()
    check(args.state_dir, args.maximum_age)
    print("healthy")


if __name__ == "__main__":
    main()

