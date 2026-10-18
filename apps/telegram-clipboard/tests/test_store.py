from pathlib import Path

import pytest

from telegram_clipboard.store import ClipboardStore


def test_store_persists_resolves_deletes_and_clears(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path / "state")
    store.initialize()
    text = store.add(
        item_id="aaaa1111-0000-0000-0000-000000000000",
        telegram_chat_id=1,
        telegram_message_id=2,
        direction="inbound",
        kind="text",
        text="hello",
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    item_id = "bbbb2222-0000-0000-0000-000000000000"
    stored = store.copy_file(source, item_id, source.name)
    image = store.add(
        item_id=item_id,
        telegram_chat_id=1,
        direction="outbound",
        kind="image",
        file_name=source.name,
        mime_type="image/png",
        local_path=str(stored),
        size_bytes=3,
    )

    assert store.latest() == image
    assert [item.id for item in store.recent()] == [image.id, text.id]
    assert store.resolve("bbbb").id == image.id
    assert store.delete("bbbb").id == image.id
    assert not stored.exists()
    assert store.clear() == 1
    assert store.latest() is None


def test_store_rejects_ambiguous_or_short_prefixes(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path / "state")
    store.initialize()
    for item_id in (
        "abcd1111-0000-0000-0000-000000000000",
        "abcd2222-0000-0000-0000-000000000000",
    ):
        store.add(
            item_id=item_id,
            telegram_chat_id=1,
            direction="inbound",
            kind="text",
            text=item_id,
        )

    with pytest.raises(ValueError):
        store.resolve("abc")
    with pytest.raises(LookupError, match="ambiguous"):
        store.resolve("abcd")


def test_read_only_reader_leaves_no_side_files(tmp_path: Path) -> None:
    import sqlite3

    state = tmp_path / "state"
    store = ClipboardStore(state)
    store.initialize()
    store.add(
        telegram_chat_id=1,
        telegram_message_id=2,
        direction="inbound",
        kind="text",
        text="hello",
    )
    # Receptionist2 reads the store read-only as a different user; a WAL
    # reader would create -wal/-shm files the bot's user cannot open.
    reader = sqlite3.connect(f"file:{store.database_path}?mode=ro", uri=True)
    assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    reader.execute("SELECT COUNT(*) FROM clipboard_items").fetchone()
    assert sorted(path.name for path in state.iterdir()) == ["clipboard.db", "items"]
    reader.close()


def test_send_can_force_document_for_images() -> None:
    from telegram_clipboard.cli import _kind

    assert _kind(Path("week5.png"), "image/png") == "image"
    assert _kind(Path("week5.png"), "image/png", as_document=True) == "document"
