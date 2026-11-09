import asyncio
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from receptionist2.config import WorkerConfig
from receptionist2.database import Database
from receptionist2.gateway import GROUP_MENU, PRIVATE_MENU, Gateway, has_fruit
from receptionist2.tombstone import SKILL, continuation_name
from receptionist2.worker import Worker

USER = 7
DM = 7
GROUP = -1001234567890
HUB = 2
REPO = Path(__file__).resolve().parents[3]


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.next_thread = 100

    async def create_forum_topic(self, chat_id, name):
        self.next_thread += 1
        self.calls.append(("create", name))
        return SimpleNamespace(message_thread_id=self.next_thread)

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send", kwargs.get("message_thread_id"), text))

    async def delete_forum_topic(self, chat_id, thread):
        self.calls.append(("delete", thread))

    async def set_my_commands(self, commands, scope):
        self.calls.append(("menu", type(scope).__name__, [c.command for c in commands]))


class Message:
    def __init__(self, text: str, thread: int | None = None) -> None:
        self.text = text
        self.message_thread_id = thread
        self.is_topic_message = thread is not None
        self.replies: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)


def setup(tmp_path: Path):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    database.set_state(f"group_hub_thread:{GROUP}", str(HUB))
    database.ensure_user(USER, DM, default_model="claude-test", default_effort="high")
    config = SimpleNamespace(
        group_chat_id=GROUP,
        allowed_user_id=USER,
        default_model="claude-test",
        default_effort="high",
        tombstone_dir=tmp_path / "tombstones",
    )
    (tmp_path / "tombstones").mkdir()
    workspace = tmp_path / "repos"
    workspace.mkdir()
    worker = Worker(
        WorkerConfig(
            state_dir=tmp_path,
            repo_root=workspace,
            timezone=ZoneInfo("America/Los_Angeles"),
            claude_binary="/usr/bin/claude",
            agent_launcher="/runner",
            poll_seconds=1,
            run_timeout_seconds=10,
            run_state_dir=tmp_path,
        ),
        database,
    )
    return Gateway(config, database), database, worker, Bot()


def command(gateway, handler, message, chat_id):
    update = SimpleNamespace(
        effective_message=message,
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=USER),
    )
    asyncio.run(handler(update, None))
    return message.replies[-1]


def press(gateway, bot, data, chat_id):
    answers, edits = [], []

    async def answer(text=None, **_kwargs):
        answers.append(text)

    async def edit_message_text(text, reply_markup=None, **_kwargs):
        buttons = (
            [b.callback_data for row in reply_markup.inline_keyboard for b in row]
            if reply_markup
            else []
        )
        edits.append((text, buttons))

    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text
        ),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=USER),
    )
    asyncio.run(gateway.tomb_button(update, SimpleNamespace(bot=bot)))
    return answers, edits


def write_tombstone(gateway, database, worker, session, chat_id, thread=None):
    reply = command(
        gateway, gateway.tombstone, Message("/tombstone", thread), chat_id
    )
    assert reply.startswith(f"🪦 Writing the tombstone for {session['display_name']}")
    run = database.claim_next_run()
    assert SKILL in run["exact_prompt"] and "Write the tombstone" in run["exact_prompt"]
    path = Path(database.get_session(session["id"])["tombstone_path"])
    assert f"TOMBSTONE_PATH={path}" in run["exact_prompt"]
    path.write_text("# tombstone\n")
    database.finish_run(run["id"], status="succeeded", final_response="saved")
    worker._offer_continuation(run)
    database.claim_outbox()  # the run's own reply
    notice = database.claim_outbox()
    assert notice["body"].startswith(f"🪦 Tombstone saved:\n{path}")
    assert notice["telegram_thread_id"] == thread
    assert f"r2:tomb:new:{session['id']}" in notice["reply_markup_json"]
    return path


def test_topic_tombstone_continues_in_a_new_topic_then_deletes(tmp_path: Path) -> None:
    gateway, database, worker, bot = setup(tmp_path)
    old = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍓 Packers ATS",
        model="claude-test", effort="high",
    )
    database.set_session_runtime(USER, model="sonnet-x", effort="max", session_id=old["id"])
    database.set_session_instructions(old["id"], "Follow the Mel skill")
    path = write_tombstone(gateway, database, worker, old, GROUP, thread=11)

    answers, edits = press(gateway, bot, f"r2:tomb:new:{old['id']}", GROUP)

    new = database.topic_session(GROUP, 101)
    assert has_fruit(new["display_name"])
    assert new["display_name"].endswith(" Packers ATS ²")
    assert (new["model"], new["effort"], new["instructions"]) == (
        "sonnet-x", "max", "Follow the Mel skill"
    )
    assert new["predecessor_session_id"] == old["id"]
    resume = database.claim_next_run()
    assert resume["session_id"] == new["id"] and resume["telegram_thread_id"] == 101
    assert "Continue from the tombstone" in resume["exact_prompt"]
    assert f"TOMBSTONE_PATH={path}" in resume["exact_prompt"]
    assert bot.calls[1][2].startswith("🪦 Continuing from 🍓 Packers ATS's tombstone")
    [(text, buttons)] = edits
    assert "https://t.me/c/1234567890/101" in text
    assert buttons == [f"r2:tomb:del:{old['id']}"]

    # A second tap does not open another chat.
    press(gateway, bot, f"r2:tomb:new:{old['id']}", GROUP)
    assert [c for c in bot.calls if c[0] == "create"] == [bot.calls[0]]

    _, edits = press(gateway, bot, f"r2:tomb:del:{old['id']}", GROUP)
    assert "removes all of its messages permanently" in edits[0][0]
    # The new chat's queued run does not block deleting the old one.
    press(gateway, bot, f"r2:tomb:delyes:{old['id']}", GROUP)
    assert ("delete", 11) in bot.calls
    assert database.get_session(old["id"])["status"] == "archived"
    assert bot.calls[-1][1] == 101 and bot.calls[-1][2].startswith("🗑 Deleted 🍓 Packers ATS")
    assert Path(path).exists()


def test_private_tombstone_continues_in_a_new_session(tmp_path: Path) -> None:
    gateway, database, worker, bot = setup(tmp_path)
    old = database.active_session(USER)
    write_tombstone(gateway, database, worker, old, DM)

    press(gateway, bot, f"r2:tomb:new:{old['id']}", DM)

    new = database.active_session(USER)
    assert new["id"] != old["id"] and new["display_name"] == f"{old['display_name']} ²"
    assert database.claim_next_run()["session_id"] == new["id"]
    answers, _ = press(gateway, bot, f"r2:tomb:delyes:{old['id']}", DM)
    assert answers[-1] == "Archived."
    assert database.get_session(old["id"])["status"] == "archived"
    # The new session is untouched and still active.
    assert database.active_session(USER)["id"] == new["id"]


def test_delete_waits_for_the_old_chats_runs(tmp_path: Path) -> None:
    gateway, database, worker, bot = setup(tmp_path)
    old = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍓 A",
        model="claude-test", effort="high",
    )
    database.enqueue_run(
        session_id=old["id"], user_id=USER, chat_id=GROUP, source="message",
        prompt="still going",
    )
    answers, _ = press(gateway, bot, f"r2:tomb:delyes:{old['id']}", GROUP)
    assert answers == ["This chat still has a queued or running run. Stop it first."]
    assert ("delete", 11) not in bot.calls


def test_missing_tombstone_file_is_reported(tmp_path: Path) -> None:
    gateway, database, worker, bot = setup(tmp_path)
    session = database.active_session(USER)
    command(gateway, gateway.tombstone, Message("/tombstone"), DM)
    run = database.claim_next_run()
    database.finish_run(run["id"], status="succeeded", final_response="done")
    worker._offer_continuation(run)
    database.claim_outbox()
    notice = database.claim_outbox()
    assert notice["body"].startswith("❌ The tombstone was not written")
    assert notice["reply_markup_json"] is None
    assert session["id"]


def test_names_menus_and_skill() -> None:
    assert continuation_name("Packers ATS") == "Packers ATS ²"
    assert continuation_name("Packers ATS ²") == "Packers ATS ³"
    assert continuation_name("Packers ATS ⁹") == "Packers ATS ¹⁰"
    assert ("tombstone", "Hand this chat off to a fresh one") in PRIVATE_MENU
    assert "tombstone" in dict(GROUP_MENU)
    skill = (REPO / SKILL.removeprefix("www/")).read_text()
    assert "## Writing" in skill and "## Resuming" in skill


def test_menus_are_set_for_private_chats_and_the_group(tmp_path: Path) -> None:
    gateway, _, _, bot = setup(tmp_path)
    asyncio.run(gateway._set_menus(bot))
    assert [call[1] for call in bot.calls] == [
        "BotCommandScopeAllPrivateChats",
        "BotCommandScopeChat",
    ]
    assert "tombstone" in bot.calls[0][2] and "tombstone" in bot.calls[1][2]


def test_a_retried_tombstone_run_still_offers_to_continue(tmp_path: Path) -> None:
    gateway, database, worker, bot = setup(tmp_path)
    session = database.active_session(USER)
    command(gateway, gateway.tombstone, Message("/tombstone"), DM)
    first = database.claim_next_run()
    database.finish_run(first["id"], status="interrupted", error="restart")
    retry = database.requeue_run(USER, first["id"])
    database.claim_next_run()
    Path(database.get_session(session["id"])["tombstone_path"]).write_text("x")
    database.finish_run(retry["id"], status="succeeded", final_response="saved")

    worker._offer_continuation(database.get_run(retry["id"]))

    bodies = []
    while (item := database.claim_outbox()) is not None:
        bodies.append(item["body"])
    assert bodies[-1].startswith("🪦 Tombstone saved:")
    # An ordinary run in the same chat offers nothing.
    other = database.enqueue_run(
        session_id=session["id"], user_id=USER, chat_id=DM, source="message",
        prompt="hello",
    )
    worker._offer_continuation(other)
    assert database.claim_outbox() is None


def test_knowledge_transfer_runs_in_the_current_chat(tmp_path: Path) -> None:
    from receptionist2.knowledge import SKILL as KT_SKILL

    gateway, database, worker, bot = setup(tmp_path)
    topic = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍓 A",
        model="claude-test", effort="high",
    )
    reply = command(
        gateway,
        gateway.knowledge_transfer,
        Message("/knowledge_transfer\nthe sandbox fix", 11),
        GROUP,
    )
    run = database.claim_next_run()
    assert run["session_id"] == topic["id"] and run["telegram_thread_id"] == 11
    assert KT_SKILL in run["exact_prompt"]
    assert run["exact_prompt"].endswith("KNOWLEDGE_TRANSFER\nFOCUS=the sandbox fix")
    assert reply.startswith("📚 Recording what 🍓 A learned")
    assert "knowledge_transfer" in dict(PRIVATE_MENU) and "knowledge_transfer" in dict(GROUP_MENU)
    for skill in (KT_SKILL, SKILL):
        assert (REPO / skill.removeprefix("www/")).is_file()
    index = (REPO / "knowledge_base" / "0index.md").read_text()
    for topic_file in (REPO / "knowledge_base").glob("*.md"):
        if topic_file.name != "0index.md":
            assert f"`{topic_file.name}`" in index
