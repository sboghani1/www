from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Config

MAX_STREAM_BYTES = 65_536
WNBA_TEMPLATE_HEADER = "WNBA_LEAN_REQUEST_V1"

ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


def redact_sensitive(text: str, *, telegram_token: str) -> str:
    redacted = text.replace(telegram_token, "[redacted]")
    redacted = re.sub(
        r"AIza[0-9A-Za-z_-]{20,}",
        "[redacted]",
        redacted,
    )
    redacted = re.sub(
        r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----",
        "[redacted]",
        redacted,
        flags=re.DOTALL,
    )
    return redacted


async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(4096):
        size += len(chunk)
        if size > limit:
            raise ValueError("WNBA helper output exceeded its limit")
        chunks.append(chunk)
    return b"".join(chunks)


class WnbaHelperClient:
    def __init__(
        self,
        config: Config,
        *,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        self.config = config
        self.process_factory = process_factory

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 16_384:
            raise ValueError("WNBA helper request is too large")
        process = await self.process_factory(
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            self.config.wnba_helper,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(encoded)
        await process.stdin.drain()
        process.stdin.close()
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, limit=MAX_STREAM_BYTES)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, limit=MAX_STREAM_BYTES)
        )
        try:
            stdout, stderr, return_code = await asyncio.wait_for(
                asyncio.gather(
                    stdout_task,
                    stderr_task,
                    process.wait(),
                ),
                timeout=self.config.wnba_helper_timeout_seconds,
            )
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(
                stdout_task, stderr_task, return_exceptions=True
            )
            raise
        stderr_text = redact_sensitive(
            stderr.decode("utf-8", errors="replace"),
            telegram_token=self.config.telegram_token,
        )
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            detail = stderr_text[-1000:] or "invalid JSON output"
            raise RuntimeError(
                f"WNBA helper failed: {detail}"
            ) from exc
        if return_code != 0 or not response.get("ok"):
            error = redact_sensitive(
                str(response.get("error") or stderr_text or "unknown error"),
                telegram_token=self.config.telegram_token,
            )
            raise RuntimeError(f"WNBA helper failed: {error[:1000]}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("WNBA helper returned an invalid result")
        return result
