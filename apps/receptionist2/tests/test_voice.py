import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from receptionist2.config import WorkerConfig
from receptionist2.database import Database
from receptionist2.gateway import NAME_FIRST_TEXT, VOICE_ASK_TEXT, Gateway
from receptionist2.voice import main as voice_main
from receptionist2.voice import transcript_preview, voice_prompt
from receptionist2.worker import Worker

USER = 7
DM = 7
GROUP = -1001234567890
HUB = 2


class TelegramFile:
    async def download_to_drive(self, custom_path) -> None:
        Path(custom_path).write_bytes(b"OggS")


class Voice:
    mime_type = "audio/ogg"
    duration = 4
    file_size = 4

    async def get_file(self):
        return TelegramFile()


class Status:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.edits: list[str] = []

    async def edit_text(self, text, **_kwargs):
        self.edits.append(text)


def buttons_of(markup):
    return (
        [b.callback_data for row in markup.inline_keyboard for b in row]
        if markup
        else []
    )


class Bot:
    def __init__(self) -> None:
        self.edits: list[tuple] = []
        self.sent: list[tuple] = []

    async def edit_message_text(self, text, *, chat_id, message_id, reply_markup=None):
        self.edits.append((chat_id, message_id, text, buttons_of(reply_markup)))

    async def send_message(self, chat_id, text, *, message_thread_id=None, reply_markup=None, **_):
        self.sent.append((chat_id, message_thread_id, text, buttons_of(reply_markup)))
        return SimpleNamespace(message_id=800 + len(self.sent))

    async def set_message_reaction(self, *_args):
        return None


class Message:
    def __init__(self, bot, message_id, *, thread=None, voice=True, text=None):
        self.bot = bot
        self.message_id = message_id
        self.message_thread_id = thread
        self.is_topic_message = thread is not None
        self.reply_to_message = None
        self.voice = Voice() if voice else None
        self.audio = None
        self.document = None
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, reply_markup=None, **_kwargs):
        self.replies.append(text)
        self.reply_buttons = buttons_of(reply_markup)
        return Status(900 + len(self.replies))

    def get_bot(self):
        return self.bot


def make_gateway(tmp_path: Path, transcript: str | None = "take the Packers"):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    database.set_state(f"group_hub_thread:{GROUP}", str(HUB))
    database.ensure_user(USER, DM, default_model="m", default_effort="high")
    config = SimpleNamespace(
        group_chat_id=GROUP,
        allowed_user_id=USER,
        default_model="claude-test",
        default_effort="high",
        images_dir=tmp_path / "images",
        voice_dir=tmp_path / "voice",
        whisper_dir=tmp_path / "whisper",
        whisper_model="small.en",
        nfl_audio_max_bytes=20 * 1024 * 1024,
        nfl_audio_max_duration_seconds=3600,
    )
    gateway = Gateway(config, database)
    calls = []

    async def run_whisper(path):
        calls.append(path)
        if transcript is None:
            return {"error": "RuntimeError: No speech was detected in the audio"}
        return {"transcript": transcript}

    gateway._run_whisper = run_whisper
    return gateway, database, calls


def update_for(message, chat_id):
    return SimpleNamespace(
        effective_message=message,
        message=message,
        callback_query=None,
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_user=SimpleNamespace(id=USER),
    )


def receive(gateway, message, chat_id=DM):
    async def scenario():
        await gateway.audio_receive(update_for(message, chat_id), None)
        await asyncio.gather(*gateway._voice_tasks)

    asyncio.run(scenario())


def press(gateway, message, data):
    answers = []

    async def answer(text=None, **_kwargs):
        answers.append(text)

    edits = []

    async def edit_message_text(text, **_kwargs):
        edits.append(text)

    async def edit_message_reply_markup(markup):
        edits.append(buttons_of(markup))

    query = SimpleNamespace(
        data=data,
        answer=answer,
        edit_message_text=edit_message_text,
        edit_message_reply_markup=edit_message_reply_markup,
        message=message,
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=USER)
    )

    async def scenario():
        await gateway.voice_button(update, SimpleNamespace(bot=message.bot))
        await asyncio.gather(*gateway._voice_tasks)

    asyncio.run(scenario())
    return answers, edits


def ask_and_transcribe(gateway, message, chat_id=DM):
    """Send audio, check the trash-first question, then tap Transcribe."""
    receive(gateway, message, chat_id)
    assert message.replies[-1] == VOICE_ASK_TEXT
    tx, trash = message.reply_buttons
    note_id = tx.rsplit(":", 1)[1]
    assert (tx, trash) == (f"r2:voice:tx:{note_id}", f"r2:voice:trash:{note_id}")
    _, edits = press(gateway, message, tx)
    assert edits == ["📝 Transcribing…"]
    return note_id


def test_voice_note_asks_first_then_transcript_arrives_as_new_message(
    tmp_path: Path,
) -> None:
    gateway, database, calls = make_gateway(tmp_path)
    bot = Bot()
    message = Message(bot, 10)

    note_id = ask_and_transcribe(gateway, message)
    receive(gateway, message)  # redelivered update: no second question

    assert message.replies == [VOICE_ASK_TEXT] and len(calls) == 1
    assert bot.edits == [(DM, 901, "🎧 Transcribed ↓", [])]
    [(chat, thread, text, buttons)] = bot.sent
    assert (chat, thread) == (DM, None)
    assert text.startswith("🎧 Heard:\n<pre>take the Packers</pre>\nTap the text")
    assert buttons == [f"r2:voice:run:{note_id}", f"r2:voice:trash:{note_id}"]
    assert press(gateway, message, f"r2:voice:tx:{note_id}")[0] == [
        "That voice note was already handled."
    ]

    _, edits = press(gateway, message, f"r2:voice:run:{note_id}")
    run = database.inbound_run(DM, 10)
    assert run["exact_prompt"] == voice_prompt("take the Packers")
    assert run["session_id"] == database.active_session(USER)["id"]
    assert database.voice_note(note_id)["run_id"] == run["id"]
    assert edits == ["🎧 Heard:\n<pre>take the Packers</pre>"]
    assert message.replies[-1].startswith("Queued · run")
    assert press(gateway, message, f"r2:voice:trash:{note_id}")[0] == [
        "That voice note was already handled."
    ]


def test_trash_before_transcribing_never_transcribes(tmp_path: Path) -> None:
    gateway, database, calls = make_gateway(tmp_path)
    message = Message(Bot(), 13)
    receive(gateway, message)
    note_id = message.reply_buttons[1].rsplit(":", 1)[1]
    path = Path(database.voice_note(note_id)["local_path"])

    _, edits = press(gateway, message, f"r2:voice:trash:{note_id}")

    assert edits == ["🗑 Trashed. The audio was deleted."] and not path.exists()
    assert calls == []


def test_transcripts_are_escaped_for_html(tmp_path: Path) -> None:
    gateway, _, _ = make_gateway(tmp_path, transcript="take <Lakers> & over")
    bot = Bot()
    ask_and_transcribe(gateway, Message(bot, 14))
    assert "<pre>take &lt;Lakers&gt; &amp; over</pre>" in bot.sent[0][2]


def test_trash_deletes_the_audio(tmp_path: Path) -> None:
    gateway, database, _ = make_gateway(tmp_path)
    bot = Bot()
    note_id = ask_and_transcribe(gateway, Message(bot, 11))
    path = Path(database.voice_note(note_id)["local_path"])
    assert path.exists()

    _, edits = press(gateway, Message(bot, 11), f"r2:voice:trash:{note_id}")

    assert edits == ["🗑 Trashed. The audio was deleted."] and not path.exists()
    assert database.inbound_run(DM, 11) is None


def test_failed_transcription_reports_and_cleans_up(tmp_path: Path) -> None:
    gateway, database, _ = make_gateway(tmp_path, transcript=None)
    bot = Bot()
    ask_and_transcribe(gateway, Message(bot, 12))

    [(_, _, text, buttons)] = bot.edits
    assert text.startswith("❌ Could not transcribe that audio: RuntimeError")
    assert buttons == [] and list((tmp_path / "voice").iterdir()) == []


def test_voice_in_a_topic_runs_in_that_chat(tmp_path: Path) -> None:
    gateway, database, _ = make_gateway(tmp_path)
    session = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍓 Mel",
        model="claude-test", effort="high",
    )
    bot = Bot()
    message = Message(bot, 20, thread=11)
    note_id = ask_and_transcribe(gateway, message, GROUP)
    assert bot.sent[0][:2] == (GROUP, 11)

    press(gateway, message, f"r2:voice:run:{note_id}")

    run = database.inbound_run(GROUP, 20)
    assert run["session_id"] == session["id"] and run["telegram_thread_id"] == 11
    # The private chat's state is untouched.
    assert database.get_user_state(USER)["telegram_chat_id"] == DM


def test_hub_audio_and_active_nfl_collections_route_elsewhere(
    tmp_path: Path, monkeypatch
) -> None:
    gateway, database, calls = make_gateway(tmp_path)
    hub = Message(Bot(), 30, thread=HUB)
    receive(gateway, hub, GROUP)
    assert hub.replies == [NAME_FIRST_TEXT]

    collected = []

    async def nfl_audio_receive(update, context):
        collected.append(update.effective_message.message_id)

    monkeypatch.setattr(
        database, "active_nfl_audio_collection", lambda *_: {"id": "c"}
    )
    gateway.nfl_audio_receive = nfl_audio_receive
    receive(gateway, Message(Bot(), 31))
    assert collected == [31] and calls == []


def test_instructions_apply_to_every_run_in_their_chat(tmp_path: Path) -> None:
    gateway, database, _ = make_gateway(tmp_path)
    session = database.create_topic_session(
        user_id=USER, chat_id=GROUP, thread_id=11, name="🍓 Mel",
        model="claude-test", effort="high",
    )

    def command(text):
        message = Message(Bot(), 40, thread=11, voice=False, text=text)
        asyncio.run(gateway.instructions(update_for(message, GROUP), None))
        return message.replies[-1]

    assert command("/instructions").startswith("This chat has no standing")
    skill = "Follow www/.claude/skills/mel/SKILL.md"
    assert command(f"/instructions {skill}").endswith(skill)
    assert command("/instructions") == f"📌 Instructions for 🍓 Mel:\n{skill}"

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
    run = {
        "session_id": session["id"],
        "telegram_thread_id": 11,
        "telegram_user_id": USER,
        "provider_session_id": "resumed",
    }
    prompt = worker._chat_context(run, "Packers -3")
    assert skill in prompt and prompt.endswith("Packers -3")

    assert command("/instructions\nAlways\nbe brief").endswith(
        "follow:\nAlways\nbe brief"
    )
    assert command("/instructions\nclear") == "📌 Instructions cleared."
    command(f"/instructions {skill}")
    assert command("/instructions clear") == "📌 Instructions cleared."
    assert worker._chat_context(run, "Packers -3") == "Packers -3"


def test_transcriber_cli_and_preview(capsys) -> None:
    assert voice_main([]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]
    assert transcript_preview("short") == "short"
    long = transcript_preview("word " * 2000)
    assert len(long) < 3400 and long.endswith("Run sends the full transcript)")


def test_a_note_whose_download_died_is_retried_on_redelivery(tmp_path: Path) -> None:
    gateway, database, calls = make_gateway(tmp_path)
    bot = Bot()
    session = database.active_session(USER)
    note, _ = database.create_voice_note(
        session_id=session["id"], user_id=USER, chat_id=DM, message_id=50
    )
    database.update_voice_note(note["id"], status_message_id=777)

    message = Message(bot, 50)
    receive(gateway, message)

    assert message.replies == [VOICE_ASK_TEXT] and calls == []
    assert database.voice_note(note["id"])["local_path"]


def test_restart_with_a_lost_file_says_so(tmp_path: Path) -> None:
    gateway, database, _ = make_gateway(tmp_path)
    bot = Bot()
    note, _ = database.create_voice_note(
        session_id=database.active_session(USER)["id"],
        user_id=USER,
        chat_id=DM,
        message_id=51,
    )
    database.update_voice_note(
        note["id"], status_message_id=778, local_path=str(tmp_path / "gone.ogg")
    )

    asyncio.run(gateway._transcribe_note(bot, database.voice_note(note["id"])))

    assert bot.edits == [
        (DM, 778, "❌ The audio was lost during a restart; send it again.", [])
    ]
    assert database.voice_note(note["id"])["status"] == "failed"


def test_old_voice_notes_table_is_rebuilt_keeping_rows(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    session = database.ensure_user(USER, DM, default_model="m", default_effort="high")
    with database.connect() as connection:
        connection.execute("DROP TABLE voice_notes")
        connection.execute(
            """
            CREATE TABLE voice_notes (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                status_message_id INTEGER, local_path TEXT, transcript TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'transcribing','ready','failed','queued','trashed'
                )),
                error TEXT, run_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(telegram_chat_id, telegram_message_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO voice_notes VALUES ('n1', ?, 7, 7, 1, NULL, '/a', "
            "'hi', 'ready', NULL, NULL, 'c', 'u')",
            (session["id"],),
        )
        connection.commit()

    database.initialize()

    assert database.voice_note("n1")["transcript"] == "hi"
    note, created = database.create_voice_note(
        session_id=session["id"], user_id=USER, chat_id=DM, message_id=2
    )
    assert created and note["status"] == "awaiting"
