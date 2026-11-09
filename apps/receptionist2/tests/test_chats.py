import asyncio
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram.error import BadRequest

from receptionist2.config import WorkerConfig
from receptionist2.database import Database
from receptionist2.gateway import (
    NEW_CHAT_NAME,
    Gateway,
    chats_text,
    provisional_title,
    topic_link,
)
from receptionist2.worker import Worker, chats_overview, clean_title

USER = 7
DM = 7
GROUP = -1001234567890
HUB = 2


def make_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "db.sqlite")
    database.initialize()
    return database


def topic(database: Database, thread: int, name: str = "Chat", **kwargs):
    return database.create_topic_session(
        user_id=USER,
        chat_id=GROUP,
        thread_id=thread,
        name=name,
        model="claude-test",
        effort="high",
        title_pending=kwargs.get("title_pending", False),
    )


def accept(database: Database, message_id: int, text: str = "hi", **kwargs):
    return database.accept_message(
        user_id=USER,
        chat_id=kwargs.pop("chat_id", GROUP if "session_id" in kwargs else DM),
        message_id=message_id,
        text=text,
        default_model="claude-test",
        default_effort="high",
        **kwargs,
    )[0]


# Database


def test_topic_runs_and_replies_stay_in_their_topic(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    session = topic(database, 11)
    assert topic(database, 11, name="Duplicate")["id"] == session["id"]

    run = accept(database, 1, session_id=session["id"])

    assert run["telegram_thread_id"] == 11 and run["session_id"] == session["id"]
    # Group messages never repoint the private chat's state.
    assert database.get_user_state(USER)["telegram_chat_id"] == DM
    assert [row["id"] for row in database.list_sessions(USER)] != [session["id"]]
    claimed = database.claim_next_run()
    database.enqueue_outbox(GROUP, "note", kind="notice", run_id=claimed["id"])
    database.finish_run(claimed["id"], status="succeeded", final_response="done")
    first = database.claim_outbox()
    second = database.claim_outbox()
    assert first["telegram_thread_id"] == second["telegram_thread_id"] == 11


def test_claim_runs_sessions_in_parallel_but_each_session_in_order(
    tmp_path: Path,
) -> None:
    database = make_db(tmp_path)
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    first = accept(database, 1)
    second = accept(database, 2)
    session = topic(database, 11)
    other = accept(database, 3, session_id=session["id"])

    assert database.claim_next_run()["id"] == first["id"]
    assert database.claim_next_run()["id"] == other["id"]
    assert database.claim_next_run() is None

    database.finish_run(
        first["id"], status="succeeded", final_response="ok",
        provider_session_id="conversation-1",
    )
    resumed = database.claim_next_run()
    # Queued before the first run finished, it still resumes its conversation.
    assert resumed["id"] == second["id"]
    assert resumed["provider_session_id"] == "conversation-1"


def test_stop_targets_the_right_chat(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    session = topic(database, 11)
    in_topic = accept(database, 1, session_id=session["id"])
    database.claim_next_run()

    assert database.request_cancel(private_only=True) is None
    assert database.request_cancel(session["id"])["id"] == in_topic["id"]


def test_topic_lifecycle_and_overview(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    blank = topic(database, 11, title_pending=True)
    assert database.blank_topic_session(GROUP, 300)["id"] == blank["id"]
    accept(database, 1, "first question", session_id=blank["id"])
    assert database.blank_topic_session(GROUP, 300) is None

    assert database.set_topic_open(GROUP, 11, False)["status"] == "archived"
    database.set_session_title(blank["id"], "Packers ATS")
    [row] = database.topic_sessions(USER)
    assert row["display_name"] == "Packers ATS" and row["title_pending"] == 0
    assert row["last_status"] == "queued" and row["last_text"] == "first question"
    assert database.first_message_text(blank["id"]) == "first question"
    assert "closed" in chats_text([row], GROUP)


def test_migration_adds_topic_columns_to_existing_database(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    with database.connect() as connection:
        connection.execute("DROP INDEX sessions_topic")
        connection.execute("ALTER TABLE outbox DROP COLUMN telegram_thread_id")
        connection.commit()
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        columns = {r["name"] for r in connection.execute("PRAGMA table_info(outbox)")}
    assert "telegram_thread_id" in columns


# Worker


def make_worker(tmp_path: Path, database: Database, **overrides) -> Worker:
    workspace = tmp_path / "repos"
    (workspace / "www").mkdir(parents=True, exist_ok=True)
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    config = WorkerConfig(
        state_dir=tmp_path / "state",
        repo_root=workspace.resolve(),
        timezone=ZoneInfo("America/Los_Angeles"),
        claude_binary="/usr/bin/claude",
        agent_launcher="/runner",
        poll_seconds=1,
        run_timeout_seconds=10,
        run_state_dir=receipts,
        allowed_user_id=USER,
        deploy_request_dir=tmp_path / "deploy-requests",
        **overrides,
    )
    return Worker(config, database)


def test_worker_runs_two_chats_at_once(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    dm = [accept(database, 1), accept(database, 2)]
    topics = [topic(database, 11), topic(database, 12)]
    others = [accept(database, 10 + i, session_id=s["id"]) for i, s in enumerate(topics)]
    worker = make_worker(tmp_path, database)
    active: list[dict] = []
    peak = 0

    async def execute(run):
        nonlocal peak
        assert run["session_id"] not in {item["session_id"] for item in active}
        active.append(run)
        peak = max(peak, len(active))
        await asyncio.sleep(0.3)
        active.remove(run)
        database.finish_run(run["id"], status="succeeded", final_response="ok")

    worker.execute = execute

    async def scenario():
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if all(
                database.get_run(run["id"])["status"] == "succeeded"
                for run in dm + others
            ):
                break
        worker.stop()
        await task

    asyncio.run(scenario())
    assert peak == 2
    assert all(
        database.get_run(run["id"])["status"] == "succeeded" for run in dm + others
    )


def test_prompts_carry_chat_context(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    session = topic(database, 11, name="Packers ATS")
    accept(database, 1, "how are the packers as dogs", session_id=session["id"])
    worker = make_worker(tmp_path, database)

    in_topic = worker._chat_context(
        database.claim_next_run(), "how are the packers as dogs"
    )
    assert "This topic: Packers ATS" in in_topic
    dm_prompt = worker._chat_context(accept(database, 2, "what's open?"), "what's open?")
    assert "- Packers ATS [running] — last message: how are the packers" in dm_prompt
    assert dm_prompt.endswith("what's open?")


def test_first_topic_run_needs_no_private_chat(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    session = topic(database, 11, name="Packers ATS")
    accept(database, 1, "question", session_id=session["id"])
    worker = make_worker(tmp_path, database)

    prompt = worker._prepare_prompt(database.claim_next_run())

    assert prompt is not None and "This topic: Packers ATS" in prompt
    assert session["id"] in prompt


def test_titles_are_cleaned() -> None:
    assert clean_title('**"Packers ATS as underdogs."**\nmore') == "Packers ATS as underdogs"
    assert len(clean_title("word " * 30)) <= 48
    assert provisional_title("  How have these teams\nbeen") == "How have these teams"
    assert provisional_title("") == NEW_CHAT_NAME
    assert topic_link(GROUP, 11) == "https://t.me/c/1234567890/11"
    assert chats_overview([]) == ""


def test_first_success_titles_the_topic_once(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    session = topic(database, 11, name="How have these…", title_pending=True)
    run = accept(database, 1, "How have these teams been ATS", session_id=session["id"])
    worker = make_worker(tmp_path, database)
    calls = []

    async def generate(message, response):
        calls.append(message)
        return "Underdog ATS records"

    worker._generate_title = generate

    async def scenario():
        worker._schedule_title(run, "answer")
        worker._schedule_title(run, "answer")
        await asyncio.gather(*worker._title_tasks)

    asyncio.run(scenario())
    assert calls == ["How have these teams been ATS"]
    assert database.get_session(session["id"])["display_name"] == "Underdog ATS records"
    item = database.claim_outbox()
    assert item["kind"] == "topic_title" and item["telegram_thread_id"] == 11


# Gateway


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.next_thread = 100
        self.missing_threads: set[int] = set()

    async def create_forum_topic(self, chat_id, name):
        self.next_thread += 1
        self.calls.append(("create", name))
        return SimpleNamespace(message_thread_id=self.next_thread)

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kwargs):
        self.calls.append(("copy", tuple(message_ids), kwargs["message_thread_id"]))

    async def send_message(self, chat_id, text, **kwargs):
        thread = kwargs.get("message_thread_id")
        if thread in self.missing_threads:
            raise BadRequest("Message thread not found")
        self.calls.append(("send", chat_id, thread, text))
        return SimpleNamespace(message_id=500)

    async def edit_forum_topic(self, chat_id, thread, name):
        self.calls.append(("rename", thread, name))

    async def reopen_forum_topic(self, chat_id, thread):
        self.calls.append(("reopen", thread))

    async def set_message_reaction(self, *_args) -> None:
        return None


class Message:
    def __init__(self, bot, message_id, text, thread=None, reply_to=None):
        self.bot = bot
        self.message_id = message_id
        self.text = text
        self.message_thread_id = thread
        self.is_topic_message = thread is not None
        self.reply_to_message = reply_to
        self.replies: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)

    def get_bot(self):
        return self.bot


def make_gateway(tmp_path: Path) -> tuple[Gateway, Database, Bot]:
    database = make_db(tmp_path)
    database.set_state(f"group_hub_thread:{GROUP}", str(HUB))
    config = SimpleNamespace(
        group_chat_id=GROUP,
        allowed_user_id=USER,
        default_model="claude-test",
        default_effort="high",
        images_dir=tmp_path / "images",
    )
    return Gateway(config, database), database, Bot()


def group_update(message):
    return SimpleNamespace(
        effective_message=message,
        message=message,
        callback_query=None,
        effective_chat=SimpleNamespace(id=GROUP, type="supergroup"),
        effective_user=SimpleNamespace(id=USER),
    )


def test_writing_in_the_hub_opens_a_topic(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)
    message = Message(bot, 40, "How have these teams been ATS as underdogs", HUB)

    asyncio.run(gateway.exact_text(group_update(message), None))
    asyncio.run(gateway.exact_text(group_update(message), None))

    run = database.inbound_run(GROUP, 40)
    session = database.get_session(run["session_id"])
    assert session["telegram_thread_id"] == 101 == run["telegram_thread_id"]
    assert session["title_pending"] == 1
    assert [call[0] for call in bot.calls] == ["create", "copy", "send"]
    assert bot.calls[2][2] == 101 and bot.calls[2][3].startswith("Queued")
    assert message.replies[0].startswith("💬 Opened How have these teams been ATS")
    assert message.replies[1].startswith("Already accepted")


def test_messages_in_topics_continue_or_adopt_them(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)
    known = topic(database, 11, name="Known")
    database.set_topic_open(GROUP, 11, False)
    created = SimpleNamespace(forum_topic_created=SimpleNamespace(name="Mine"))

    asyncio.run(gateway.exact_text(group_update(Message(bot, 50, "more", 11)), None))
    asyncio.run(
        gateway.exact_text(group_update(Message(bot, 51, "hi", 12, created)), None)
    )

    assert database.inbound_run(GROUP, 50)["session_id"] == known["id"]
    assert database.get_session(known["id"])["status"] == "active"
    assert ("reopen", 11) in bot.calls
    adopted = database.topic_session(GROUP, 12)
    assert adopted["display_name"] == "Mine" and not adopted["title_pending"]
    assert not any(call[0] == "create" for call in bot.calls)


def test_private_only_commands_are_refused_in_the_group(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)
    message = Message(bot, 60, "/reset", 11)
    handled = []

    async def handler(update, context):
        handled.append(update)

    asyncio.run(gateway.authorized(handler)(group_update(message), None))
    asyncio.run(gateway.authorized(handler, group=True)(group_update(message), None))
    stranger = group_update(message)
    stranger.effective_chat = SimpleNamespace(id=-100999, type="supergroup")
    asyncio.run(gateway.authorized(handler, group=True)(stranger, None))

    assert message.replies == ["That works only in the private chat with me."]
    assert len(handled) == 1
    # The group never repoints the private chat.
    with database.connect() as connection:
        users = connection.execute("SELECT COUNT(*) FROM user_state").fetchone()[0]
    assert users == 0


def test_new_chat_button_reuses_a_blank_topic(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)
    answers = []

    async def answer(text=None, **_kwargs):
        answers.append(text)

    query = SimpleNamespace(answer=answer, message=Message(bot, 1, "", HUB))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=USER)
    )
    context = SimpleNamespace(bot=bot)

    asyncio.run(gateway.new_chat_button(update, context))
    asyncio.run(gateway.new_chat_button(update, context))

    assert [call[0] for call in bot.calls].count("create") == 1
    assert answers == [f"Opened {NEW_CHAT_NAME}"] * 2
    assert bot.calls[-1][2] == HUB


def test_topic_service_messages_update_sessions(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)

    def event(**kwargs):
        message = SimpleNamespace(
            message_thread_id=21,
            forum_topic_created=None,
            forum_topic_edited=None,
            forum_topic_closed=None,
            forum_topic_reopened=None,
        )
        for key, value in kwargs.items():
            setattr(message, key, value)
        asyncio.run(gateway.topic_event(group_update(message), None))

    event(forum_topic_created=SimpleNamespace(name="Draft"))
    event(forum_topic_edited=SimpleNamespace(name="Renamed"))
    event(forum_topic_closed=True)
    session = database.topic_session(GROUP, 21)
    assert session["display_name"] == "Renamed" and session["status"] == "archived"
    event(forum_topic_reopened=True)
    assert database.topic_session(GROUP, 21)["status"] == "active"


def test_delivery_targets_topics_and_survives_deleted_ones(tmp_path: Path) -> None:
    gateway, database, bot = make_gateway(tmp_path)
    database.enqueue_outbox(GROUP, "Better name", kind="topic_title", thread_id=11)
    database.enqueue_outbox(GROUP, "answer", kind="notice", thread_id=11)
    database.enqueue_outbox(GROUP, "orphan", kind="notice", thread_id=12)
    database.enqueue_outbox(DM, "private", kind="notice")
    bot.missing_threads = {12}

    asyncio.run(gateway.deliver_outbox(SimpleNamespace(bot=bot)))

    assert bot.calls == [
        ("rename", 11, "Better name"),
        ("send", GROUP, 11, "answer"),
        ("send", USER, None, "(from a deleted chat topic)\norphan"),
        ("send", DM, None, "private"),
    ]
    assert database.claim_outbox() is None
