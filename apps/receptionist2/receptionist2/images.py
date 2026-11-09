"""Telegram images (usually phone or laptop screenshots) as agent input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Telegram bots cannot download files larger than 20 MB.
IMAGE_MAX_BYTES = 20 * 1024 * 1024
# Album items arrive as separate messages; wait this long after the last one.
ALBUM_SETTLE_SECONDS = 2.5
# Formats Claude Code's Read tool can view.
IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
# Adapted from the forwarder fleet's no-comment share nudge.
NO_CAPTION_REQUEST = (
    "The user shared this with no comment. Work out from the image what it "
    "is about. If what they want is clear, do it; otherwise ask, briefly."
)


@dataclass(frozen=True)
class TelegramImage:
    media: Any
    suffix: str
    size_bytes: int | None


def telegram_image(message: Any) -> TelegramImage | None:
    """The image to download, or None for an unsupported image document."""
    if message.photo:
        # Telegram lists compressed JPEG sizes smallest first.
        largest = message.photo[-1]
        return TelegramImage(largest, ".jpg", largest.file_size)
    document = message.document
    suffix = IMAGE_SUFFIXES.get(getattr(document, "mime_type", None) or "")
    if document is None or suffix is None:
        return None
    return TelegramImage(document, suffix, document.file_size)


def image_prompt(caption: str, paths: list[str]) -> str:
    noun = "image" if len(paths) == 1 else f"{len(paths)} images"
    listing = "\n".join(f"- {path}" for path in paths)
    return (
        f"{caption.strip() or NO_CAPTION_REQUEST}\n\n"
        f"[Attached {noun}, usually a phone or laptop screenshot. Open each "
        f"with the Read tool before answering:\n{listing}]"
    )


def image_summary(caption: str, count: int) -> str:
    """The stored message text, which also feeds topic tracking."""
    label = "📷 image" if count == 1 else f"📷 {count} images"
    return f"{label}: {caption.strip()}" if caption.strip() else label
