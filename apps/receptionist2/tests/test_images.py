import asyncio
import os
import stat
from types import SimpleNamespace

from receptionist2.database import Database
from receptionist2.gateway import SHARED_DIR_MODE, Gateway
from receptionist2.images import image_prompt, image_summary, telegram_image
from receptionist2.maintenance import run_maintenance

CHAT = 42


class TelegramFile:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download_to_drive(self, custom_path) -> None:
        with open(custom_path, "wb") as handle:
            handle.write(self.data)


class Media:
    def __init__(self, data: bytes = b"\x89PNG", mime_type: str | None = None):
        self.data = data
        self.file_size = len(data)
        self.mime_type = mime_type

    async def get_file(self) -> TelegramFile:
        return TelegramFile(self.data)


class Bot:
    async def set_message_reaction(self, *_args) -> None:
        return None


class Message:
    def __init__(
        self,
        message_id: int,
        *,
        caption: str | None = None,
        photo=None,
        document=None,
        media_group_id: str | None = None,
    ) -> None:
        self.message_id = message_id
        self.caption = caption
        self.photo = photo or []
        self.document = document
        self.media_group_id = media_group_id
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)

    def get_bot(self) -> Bot:
        return Bot()


class JobQueue:
    def __init__(self) -> None:
        self.scheduled = []

    def get_jobs_by_name(self, name):
        return [job for job in self.scheduled if job.name == name]

    def run_once(self, callback, _when, *, data, name):
        job = SimpleNamespace(
            callback=callback, data=data, name=name, removed=False
        )
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self.scheduled.append(job)


def make_gateway(tmp_path) -> Gateway:
    database = Database(tmp_path / "receptionist2.db")
    database.initialize()
    config = SimpleNamespace(
        images_dir=tmp_path / "images",
        default_model="claude-test",
        default_effort="high",
        group_chat_id=0,
    )
    return Gateway(config, database)


def send(gateway: Gateway, message: Message, jobs: JobQueue | None = None):
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=CHAT),
        effective_user=SimpleNamespace(id=7),
    )
    context = SimpleNamespace(job_queue=jobs or JobQueue())
    asyncio.run(gateway.image_receive(update, context))


def test_screenshot_becomes_a_run_that_reads_the_image(tmp_path) -> None:
    gateway = make_gateway(tmp_path)
    message = Message(
        10, caption="what does this error mean?", photo=[Media(b"s"), Media()]
    )

    send(gateway, message)

    run = gateway.database.inbound_run(CHAT, 10)
    [image] = list((tmp_path / "images").iterdir())
    assert run["exact_prompt"] == image_prompt(
        "what does this error mean?", [str(image)]
    )
    assert image.read_bytes() == b"\x89PNG"
    assert stat.S_IMODE(image.stat().st_mode) == 0o660
    assert message.replies[-1].startswith("Queued · run")

    send(gateway, message)
    assert message.replies[-1].startswith("Already accepted")
    assert len(list((tmp_path / "images").iterdir())) == 1


def test_album_becomes_one_run_with_every_image(tmp_path) -> None:
    gateway = make_gateway(tmp_path)
    jobs = JobQueue()
    messages = [
        Message(21, photo=[Media()], media_group_id="g"),
        Message(20, caption="compare these", photo=[Media()], media_group_id="g"),
        Message(22, document=Media(mime_type="image/png"), media_group_id="g"),
    ]
    for message in messages:
        send(gateway, message, jobs)

    live = [job for job in jobs.scheduled if not job.removed]
    assert len(live) == 1 and gateway.database.inbound_run(CHAT, 20) is None
    asyncio.run(live[0].callback(SimpleNamespace(job=live[0])))

    run = gateway.database.inbound_run(CHAT, 20)
    prompt = run["exact_prompt"]
    assert prompt.startswith("compare these\n\n[Attached 3 images")
    assert prompt.count(str(tmp_path / "images")) == 3
    assert messages[1].replies[-1].startswith("Queued · run")


def test_unsupported_image_document_is_refused(tmp_path) -> None:
    gateway = make_gateway(tmp_path)
    message = Message(30, document=Media(mime_type="image/heic"))

    send(gateway, message)

    assert "PNG, JPEG, WebP, or GIF" in message.replies[-1]
    assert gateway.database.inbound_run(CHAT, 30) is None


def test_image_helpers() -> None:
    assert telegram_image(Message(1, document=Media(mime_type="image/png"))).suffix == ".png"
    assert telegram_image(Message(1, document=Media(mime_type="application/pdf"))) is None
    assert image_prompt("", ["/a.png"]).startswith("The user shared this with no comment.")
    assert image_summary("", 1) == "📷 image"
    assert image_summary(" why? ", 2) == "📷 2 images: why?"


def test_maintenance_removes_old_images(tmp_path) -> None:
    Database(tmp_path / "receptionist2.db").initialize()
    images = tmp_path / "images"
    images.mkdir()
    old, fresh = images / "old.png", images / "fresh.png"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (1_000_000.0, 1_000_000.0))

    result = run_maintenance(tmp_path, now=1_000_000.0 + 15 * 86400)

    assert result["images_removed"] == 1
    assert not old.exists() and fresh.exists()


def test_images_save_under_the_gateway_sandbox(tmp_path, monkeypatch) -> None:
    # The gateway unit sets RestrictSUIDSGID=true: set-gid modes fail EPERM.
    real_mkdir, real_chmod = os.mkdir, os.chmod

    def no_setgid(real):
        def call(path, mode=0o777, *args, **kwargs):
            if mode & stat.S_ISGID:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real(path, mode, *args, **kwargs)

        return call

    monkeypatch.setattr(os, "mkdir", no_setgid(real_mkdir))
    monkeypatch.setattr(os, "chmod", no_setgid(real_chmod))
    gateway = make_gateway(tmp_path)
    message = Message(30, caption="why?", photo=[Media()])

    send(gateway, message)

    assert message.replies[-1].startswith("Queued · run")
    directory = tmp_path / "images"
    assert stat.S_IMODE(directory.stat().st_mode) == SHARED_DIR_MODE
    assert not SHARED_DIR_MODE & (stat.S_ISGID | stat.S_ISUID)
