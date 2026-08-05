from datetime import UTC, datetime, timedelta
from pathlib import Path

from receptionist.config import RepositoryConfig
from receptionist.database import Database
from receptionist.maintenance import run_maintenance


def test_maintenance_backs_up_database_and_prunes_old_backup(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repos" / "www"
    repository.mkdir(parents=True)
    state_dir = tmp_path / "state"
    database = Database(state_dir / "receptionist.db")
    database.initialize((RepositoryConfig("www", repository),))
    backup_dir = state_dir / "backups"
    backup_dir.mkdir()
    old_backup = backup_dir / "receptionist-old.db"
    old_backup.write_bytes(b"old")
    old_time = (datetime.now(UTC) - timedelta(days=20)).timestamp()
    old_backup.touch()
    import os

    os.utime(old_backup, (old_time, old_time))

    backup = run_maintenance(state_dir, backup_days=14)

    assert backup.is_file()
    assert not old_backup.exists()
