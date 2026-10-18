from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


KINDS = {"text", "image", "document", "video"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ClipboardItem:
    id: str
    telegram_message_id: int | None
    telegram_chat_id: int
    direction: str
    kind: str
    text: str
    caption: str
    file_name: str
    mime_type: str
    telegram_file_id: str
    telegram_file_unique_id: str
    local_path: str
    size_bytes: int | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ClipboardStore:
    def __init__(self, state_dir: Path) -> None:
        if not state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        self.state_dir = state_dir
        self.database_path = state_dir / "clipboard.db"
        self.items_dir = state_dir / "items"

    def initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.items_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o770)
        os.chmod(self.items_dir, 0o770)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS clipboard_items (
                    id TEXT PRIMARY KEY,
                    telegram_message_id INTEGER,
                    telegram_chat_id INTEGER NOT NULL,
                    direction TEXT NOT NULL
                        CHECK(direction IN ('inbound','outbound')),
                    kind TEXT NOT NULL
                        CHECK(kind IN ('text','image','document','video')),
                    text TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    telegram_file_id TEXT NOT NULL DEFAULT '',
                    telegram_file_unique_id TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS clipboard_items_created
                    ON clipboard_items(created_at DESC);
                """
            )
            connection.commit()
        for path in (
            self.database_path,
            self.database_path.with_name(self.database_path.name + "-wal"),
            self.database_path.with_name(self.database_path.name + "-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o660)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def item_path(self, item_id: str, file_name: str) -> Path:
        safe_name = Path(file_name or "clipboard-item").name
        directory = self.items_dir / item_id
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, 0o770)
        return directory / safe_name

    def copy_file(self, source: Path, item_id: str, file_name: str) -> Path:
        destination = self.item_path(item_id, file_name)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o660)
        os.replace(temporary, destination)
        return destination

    def add(
        self,
        *,
        telegram_chat_id: int,
        direction: str,
        kind: str,
        telegram_message_id: int | None = None,
        text: str = "",
        caption: str = "",
        file_name: str = "",
        mime_type: str = "",
        telegram_file_id: str = "",
        telegram_file_unique_id: str = "",
        local_path: str = "",
        size_bytes: int | None = None,
        item_id: str | None = None,
    ) -> ClipboardItem:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("direction is invalid")
        if kind not in KINDS:
            raise ValueError("kind is invalid")
        item = ClipboardItem(
            id=item_id or str(uuid.uuid4()),
            telegram_message_id=telegram_message_id,
            telegram_chat_id=telegram_chat_id,
            direction=direction,
            kind=kind,
            text=text,
            caption=caption,
            file_name=file_name,
            mime_type=mime_type,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            local_path=local_path,
            size_bytes=size_bytes,
            created_at=utc_now(),
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clipboard_items(
                    id, telegram_message_id, telegram_chat_id, direction,
                    kind, text, caption, file_name, mime_type,
                    telegram_file_id, telegram_file_unique_id, local_path,
                    size_bytes, created_at
                ) VALUES (
                    :id, :telegram_message_id, :telegram_chat_id, :direction,
                    :kind, :text, :caption, :file_name, :mime_type,
                    :telegram_file_id, :telegram_file_unique_id, :local_path,
                    :size_bytes, :created_at
                )
                """,
                item.to_dict(),
            )
            connection.commit()
        return item

    def latest(self) -> ClipboardItem | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM clipboard_items "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return ClipboardItem(**dict(row)) if row else None

    def recent(self, limit: int = 20) -> list[ClipboardItem]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be 1-100")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clipboard_items "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ClipboardItem(**dict(row)) for row in rows]

    def resolve(self, prefix: str) -> ClipboardItem:
        normalized = prefix.strip().lower()
        if len(normalized) < 4:
            raise ValueError("Item prefix must contain at least 4 characters")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clipboard_items "
                "WHERE lower(id) LIKE ? ORDER BY created_at DESC LIMIT 2",
                (normalized + "%",),
            ).fetchall()
        if not rows:
            raise LookupError("Clipboard item not found")
        if len(rows) > 1:
            raise LookupError("Clipboard item prefix is ambiguous")
        return ClipboardItem(**dict(rows[0]))

    def delete(self, prefix: str) -> ClipboardItem:
        item = self.resolve(prefix)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM clipboard_items WHERE id=?", (item.id,)
            )
            connection.commit()
        self._remove_local_item(item)
        return item

    def clear(self) -> int:
        with self.connect() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM clipboard_items"
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM clipboard_items")
            connection.commit()
        if self.items_dir.exists():
            for child in self.items_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        return count

    def _remove_local_item(self, item: ClipboardItem) -> None:
        if not item.local_path:
            return
        path = Path(item.local_path)
        try:
            path.relative_to(self.items_dir)
        except ValueError:
            return
        directory = path.parent
        if directory.is_dir():
            shutil.rmtree(directory)

    def latest_json(self) -> str:
        item = self.latest()
        return json.dumps(
            item.to_dict() if item else None,
            ensure_ascii=False,
            sort_keys=True,
        )
