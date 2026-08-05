from __future__ import annotations

import io
from collections.abc import Iterable

TELEGRAM_TEXT_LIMIT = 3900
ATTACHMENT_THRESHOLD = 12_000


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def text_attachment(text: str, filename: str) -> io.BytesIO:
    payload = io.BytesIO(text.encode("utf-8"))
    payload.name = filename
    return payload


def compact_lines(lines: Iterable[str], maximum: int = 6) -> str:
    values = [line.strip() for line in lines if line.strip()]
    return "\n".join(values[-maximum:])

