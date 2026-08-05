from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from telegram import Bot


async def discover(token: str, claim_code: str, timeout: int) -> int:
    bot = Bot(token)
    deadline = asyncio.get_running_loop().time() + timeout
    offset: int | None = None
    print(
        f"Send this exact private message to the new bot: /claim {claim_code}",
        flush=True,
    )
    while asyncio.get_running_loop().time() < deadline:
        updates = await bot.get_updates(
            offset=offset, timeout=20, allowed_updates=["message"]
        )
        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if (
                message
                and message.chat.type == "private"
                and message.text == f"/claim {claim_code}"
                and message.from_user
            ):
                await message.reply_text("Identity claimed. Setup can continue.")
                return message.from_user.id
    raise TimeoutError("No matching claim message arrived before timeout.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--claim-code", default=secrets.token_urlsafe(12))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        user_id = asyncio.run(discover(args.token, args.claim_code, args.timeout))
    except TimeoutError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    print(user_id)


if __name__ == "__main__":
    main()

