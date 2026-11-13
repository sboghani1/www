import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram.error import BadRequest

from receptionist2.database import Database
from receptionist2.gateway import Gateway

USER = 7
DM = 7


class Bot:
    def __init__(self, fail_edit: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_edit = fail_edit

    async def edit_message_text(self, text, *, chat_id, message_id, reply_markup=None):
        if self.fail_edit:
            raise BadRequest(self.fail_edit)
        self.calls.append(("edit", message_id, text))

    async def send_message(self, chat_id, text, **_kwargs):
        self.calls.append(("send", chat_id, text))
        return SimpleNamespace(message_id=600)

    async def set_message_reaction(self, *_args):
        return None


class Message:
    def __init__(self, bot, message_id: int) -> None:
        self.bot = bot
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=500 + self.message_id)

    def get_bot(self):
        return self.bot


def setup(tmp_path: Path, bot: Bot):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    config = SimpleNamespace(
        group_chat_id=0,
        allowed_user_id=USER,
        default_model="claude-test",
        default_effort="high",
    )
    return Gateway(config, database), database


def accept(gateway, bot, message_id: int, text: str = "hi"):
    return asyncio.run(
        gateway._accept_text(
            Message(bot, message_id),
            user_id=USER,
            chat_id=DM,
            message_id=message_id,
            text=text,
        )
    )


def finish_and_deliver(gateway, database, bot, run, answer):
    database.claim_next_run()
    database.finish_run(run["id"], status="succeeded", final_response=answer)
    asyncio.run(gateway.deliver_outbox(SimpleNamespace(bot=bot)))


def test_the_queued_reply_becomes_the_answer(tmp_path: Path) -> None:
    bot = Bot()
    gateway, database = setup(tmp_path, bot)
    run = accept(gateway, bot, 10)
    assert database.get_run(run["id"])["status_message_id"] == 510

    # A resent update does not take over the status message.
    accept(gateway, bot, 10)
    assert database.get_run(run["id"])["status_message_id"] == 510

    finish_and_deliver(gateway, database, bot, run, "Raptors -2.5")

    assert bot.calls == [("edit", 510, f"🔖 {run['id'][:8]}\n\nRaptors -2.5")]
    assert database.claim_outbox() is None


def test_a_long_answer_overflows_into_new_messages(tmp_path: Path) -> None:
    bot = Bot()
    gateway, database = setup(tmp_path, bot)
    run = accept(gateway, bot, 11)
    answer = "x" * 5000

    finish_and_deliver(gateway, database, bot, run, answer)

    (kind, message_id, first), (kind2, _, rest) = bot.calls
    assert (kind, message_id, kind2) == ("edit", 511, "send")
    assert first + rest == f"🔖 {run['id'][:8]}\n\n{answer}"


def test_failed_edit_falls_back_to_a_new_message(tmp_path: Path) -> None:
    bot = Bot(fail_edit="Message to edit not found")
    gateway, database = setup(tmp_path, bot)
    run = accept(gateway, bot, 12)

    finish_and_deliver(gateway, database, bot, run, "answer")

    assert bot.calls == [("send", DM, f"🔖 {run['id'][:8]}\n\nanswer")]


def test_progress_never_overwrites_a_finished_answer(tmp_path: Path) -> None:
    bot = Bot()
    gateway, database = setup(tmp_path, bot)
    run = accept(gateway, bot, 13)
    claimed = database.claim_next_run()
    stale = [dict(claimed, status_message_id=513)]
    database.finish_run(run["id"], status="succeeded", final_response="done")
    database.running_runs = lambda: stale  # the race: read before it finished

    asyncio.run(gateway.update_progress(SimpleNamespace(bot=bot)))

    assert bot.calls == []
