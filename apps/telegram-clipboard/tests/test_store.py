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
