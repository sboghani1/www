import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram_clipboard.bot import ClipboardBot, summary
from telegram_clipboard.config import Config
from telegram_clipboard.store import ClipboardStore


def make_bot(tmp_path: Path) -> ClipboardBot:
    return ClipboardBot(
        Config(
            bot_token="token",
            allowed_user_id=1,
            expected_username="clipboard2_bot",
            state_dir=tmp_path / "state",
            max_download_bytes=1024,
        ),
        ClipboardStore(tmp_path / "state"),
    )


def test_receive_text_and_clear_confirmation(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    bot.store.initialize()
    message = SimpleNamespace(
        message_id=4,
        text="copy me",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1),
        message=message,
    )

    asyncio.run(bot.receive_text(update, SimpleNamespace()))

    assert bot.store.latest().text == "copy me"
    assert "Stored text" in message.reply_text.await_args.args[0]

    asyncio.run(bot.clear(update, SimpleNamespace(args=[])))
    assert bot.store.latest() is not None
    asyncio.run(bot.clear(update, SimpleNamespace(args=["confirm"])))
    assert bot.store.latest() is None


def test_summary_is_bounded(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path / "state")
    store.initialize()
    item = store.add(
        telegram_chat_id=1,
        direction="inbound",
        kind="text",
        text="x" * 200,
    )

    rendered = summary(item)

    assert rendered.startswith(item.id[:8])
    assert len(rendered) < 130
