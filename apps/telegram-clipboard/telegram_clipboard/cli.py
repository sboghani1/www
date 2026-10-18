from __future__ import annotations

import argparse
import asyncio
import mimetypes
from pathlib import Path

from telegram import Bot

from .config import Config
from .store import ClipboardStore


def _kind(path: Path, mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "document"


async def _send(args: argparse.Namespace) -> None:
    config = Config.from_env()
    store = ClipboardStore(config.state_dir)
    store.initialize()
    bot = Bot(config.bot_token)
    if args.text is not None:
        message = await bot.send_message(
            chat_id=config.allowed_user_id,
            text=args.text,
        )
        item = store.add(
            telegram_chat_id=config.allowed_user_id,
            telegram_message_id=message.message_id,
            direction="outbound",
            kind="text",
            text=args.text,
        )
        print(item.id)
        return
    path = args.path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size > config.max_download_bytes:
        raise ValueError(
            f"File exceeds {config.max_download_bytes} byte clipboard limit"
        )
    mime_type = mimetypes.guess_type(path.name)[0] or (
        "application/octet-stream"
    )
    kind = _kind(path, mime_type)
    with path.open("rb") as handle:
        if kind == "image":
            message = await bot.send_photo(
                chat_id=config.allowed_user_id,
                photo=handle,
                caption=args.caption or None,
            )
            media = message.photo[-1]
        elif kind == "video":
            message = await bot.send_video(
                chat_id=config.allowed_user_id,
                video=handle,
                caption=args.caption or None,
            )
            media = message.video
        else:
            message = await bot.send_document(
                chat_id=config.allowed_user_id,
                document=handle,
                filename=path.name,
                caption=args.caption or None,
            )
            media = message.document
    item_id = __import__("uuid").uuid4().hex
    stored_path = store.copy_file(path, item_id, path.name)
    item = store.add(
        item_id=item_id,
        telegram_chat_id=config.allowed_user_id,
        telegram_message_id=message.message_id,
        direction="outbound",
        kind=kind,
        caption=args.caption or "",
        file_name=path.name,
        mime_type=mime_type,
        telegram_file_id=media.file_id,
        telegram_file_unique_id=media.file_unique_id,
        local_path=str(stored_path),
        size_bytes=size,
    )
    print(item.id)


def send_main() -> None:
    parser = argparse.ArgumentParser(
        description="Send and persist a clipboard item."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("path", type=Path, nargs="?")
    group.add_argument("--text")
    parser.add_argument("--caption", default="")
    asyncio.run(_send(parser.parse_args()))


def latest_main() -> None:
    config = Config.from_env()
    store = ClipboardStore(config.state_dir)
    store.initialize()
    print(store.latest_json())
