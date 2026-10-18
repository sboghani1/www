from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .store import ClipboardItem, ClipboardStore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram_clipboard")
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
HELP_TEXT = (
    "Send text, an image, a document, or a video to store it persistently.\n\n"
    "/latest - resend the latest item\n"
    "/list - show the 20 newest items\n"
    "/get <id-prefix> - resend one item\n"
    "/delete <id-prefix> - delete one item\n"
    "/clear confirm - delete every item"
)


def summary(item: ClipboardItem) -> str:
    detail = item.text or item.caption or item.file_name or item.mime_type
    detail = " ".join(detail.split())
    if len(detail) > 80:
        detail = detail[:77] + "..."
    return (
        f"{item.id[:8]} · {item.kind} · {item.direction}"
        + (f" · {detail}" if detail else "")
    )


class ClipboardBot:
    def __init__(self, config: Config, store: ClipboardStore) -> None:
        self.config = config
        self.store = store

    def authorized(self, handler: Handler) -> Handler:
        async def wrapped(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
        ) -> None:
            user = update.effective_user
            chat = update.effective_chat
            if (
                user is None
                or chat is None
                or chat.type != ChatType.PRIVATE
                or user.id != self.config.allowed_user_id
            ):
                log.warning(
                    "Rejected clipboard update user=%s chat=%s",
                    user.id if user else None,
                    chat.type if chat else None,
                )
                return
            await handler(update, context)

        return wrapped

    async def post_init(self, application: Application) -> None:
        self.store.initialize()
        identity = await application.bot.get_me()
        if identity.username != self.config.expected_username:
            raise RuntimeError(
                "Clipboard bot identity mismatch: "
                f"expected @{self.config.expected_username}, "
                f"got @{identity.username}"
            )
        await application.bot.set_my_commands(
            [
                BotCommand("latest", "Resend the latest item"),
                BotCommand("list", "List recent clipboard items"),
                BotCommand("get", "Resend one item by ID prefix"),
                BotCommand("delete", "Delete one item by ID prefix"),
                BotCommand("clear", "Delete every stored item"),
                BotCommand("help", "Show clipboard help"),
            ]
        )

    async def start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await update.message.reply_text(
            "📋 Clipboard ready.\n\n" + HELP_TEXT
        )

    async def help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await update.message.reply_text(HELP_TEXT)

    async def receive_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        item = self.store.add(
            telegram_chat_id=update.effective_chat.id,
            telegram_message_id=update.message.message_id,
            direction="inbound",
            kind="text",
            text=update.message.text,
        )
        await update.message.reply_text(f"📋 Stored text `{item.id[:8]}`.")

    async def receive_media(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        message = update.message
        if message.photo:
            media = message.photo[-1]
            kind = "image"
            file_name = f"photo-{message.message_id}.jpg"
            mime_type = "image/jpeg"
        elif message.document:
            media = message.document
            kind = "document"
            file_name = message.document.file_name or (
                f"document-{message.message_id}"
            )
            mime_type = message.document.mime_type or (
                "application/octet-stream"
            )
        elif message.video:
            media = message.video
            kind = "video"
            file_name = message.video.file_name or (
                f"video-{message.message_id}.mp4"
            )
            mime_type = message.video.mime_type or "video/mp4"
        else:
            await message.reply_text("Unsupported clipboard media.")
            return
        size = media.file_size
        if size is not None and size > self.config.max_download_bytes:
            await message.reply_text(
                "That item exceeds the clipboard download limit of "
                f"{self.config.max_download_bytes // (1024 * 1024)} MB."
            )
            return
        item_id = __import__("uuid").uuid4().hex
        destination = self.store.item_path(item_id, file_name)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        telegram_file = await media.get_file()
        await telegram_file.download_to_drive(custom_path=temporary)
        actual_size = temporary.stat().st_size
        if actual_size > self.config.max_download_bytes:
            temporary.unlink(missing_ok=True)
            destination.parent.rmdir()
            await message.reply_text("Downloaded item exceeds the size limit.")
            return
        temporary.chmod(0o660)
        temporary.replace(destination)
        item = self.store.add(
            item_id=item_id,
            telegram_chat_id=update.effective_chat.id,
            telegram_message_id=message.message_id,
            direction="inbound",
            kind=kind,
            caption=message.caption or "",
            file_name=Path(file_name).name,
            mime_type=mime_type,
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
            local_path=str(destination),
            size_bytes=actual_size,
        )
        await message.reply_text(
            f"📋 Stored {kind} `{item.id[:8]}` ({actual_size} bytes)."
        )

    async def _send_item(
        self,
        update: Update,
        item: ClipboardItem,
    ) -> None:
        if item.kind == "text":
            await update.message.reply_text(item.text or "(empty text)")
            return
        path = Path(item.local_path)
        if not path.is_file():
            await update.message.reply_text(
                f"Stored file for `{item.id[:8]}` is missing."
            )
            return
        with path.open("rb") as handle:
            if item.kind == "image":
                await update.message.reply_photo(
                    photo=handle, caption=item.caption or None
                )
            elif item.kind == "video":
                await update.message.reply_video(
                    video=handle, caption=item.caption or None
                )
            else:
                await update.message.reply_document(
                    document=handle,
                    caption=item.caption or None,
                    filename=item.file_name or path.name,
                )

    async def latest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        item = self.store.latest()
        if item is None:
            await update.message.reply_text("Clipboard is empty.")
            return
        await self._send_item(update, item)

    async def list_items(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        items = self.store.recent()
        await update.message.reply_text(
            "\n".join(summary(item) for item in items)
            if items
            else "Clipboard is empty."
        )

    async def get_item(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /get <id-prefix>")
            return
        try:
            item = self.store.resolve(context.args[0])
        except (LookupError, ValueError) as error:
            await update.message.reply_text(str(error))
            return
        await self._send_item(update, item)

    async def delete_item(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /delete <id-prefix>")
            return
        try:
            item = self.store.delete(context.args[0])
        except (LookupError, ValueError) as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(f"Deleted `{item.id[:8]}`.")

    async def clear(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if context.args != ["confirm"]:
            await update.message.reply_text(
                "This permanently deletes every item. Use /clear confirm."
            )
            return
        count = self.store.clear()
        await update.message.reply_text(f"Cleared {count} clipboard item(s).")

    def application(self) -> Application:
        application = (
            Application.builder()
            .token(self.config.bot_token)
            .post_init(self.post_init)
            .build()
        )
        application.add_handler(
            CommandHandler("start", self.authorized(self.start))
        )
        application.add_handler(
            CommandHandler("help", self.authorized(self.help))
        )
        application.add_handler(
            CommandHandler("latest", self.authorized(self.latest))
        )
        application.add_handler(
            CommandHandler("list", self.authorized(self.list_items))
        )
        application.add_handler(
            CommandHandler("get", self.authorized(self.get_item))
        )
        application.add_handler(
            CommandHandler("delete", self.authorized(self.delete_item))
        )
        application.add_handler(
            CommandHandler("clear", self.authorized(self.clear))
        )
        application.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.Document.ALL | filters.VIDEO),
                self.authorized(self.receive_media),
            )
        )
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.authorized(self.receive_text),
            )
        )
        return application


def main() -> None:
    config = Config.from_env()
    ClipboardBot(config, ClipboardStore(config.state_dir)).application().run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
