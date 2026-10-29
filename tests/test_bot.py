from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import events
from telethon.errors import FloodWaitError, UserNotParticipantError
from telethon.tl.types import ChannelParticipant

from doctovid import media
from doctovid.bot import (
    MAX_OPEN_MENUS,
    THUMBNAIL_ATTEMPTS_PER_HOUR,
    THUMBNAIL_MAX_BYTES,
    THUMBNAIL_SLOTS,
    Bot,
)
from doctovid.config import Config, load
from doctovid.jobs import Job, Mode
from doctovid.store import Store

from .conftest import FA, MakeClip, bare, make_request, needs_ffmpeg


@dataclass
class Sent:
    chat_id: int
    text: str
    buttons: Any = None
    id: int = 0


@dataclass
class FakeClient:
    """The few Telethon calls the bot makes itself, recorded."""

    sent: list[Sent] = field(default_factory=list)
    edits: dict[int, str] = field(default_factory=dict)
    deleted: list[int] = field(default_factory=list)
    ids: itertools.count[int] = field(default_factory=lambda: itertools.count(500))

    def add_event_handler(self, *args: Any) -> None:
        pass

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Sent:
        message = Sent(chat_id, text, kwargs.get("buttons"), next(self.ids))
        self.sent.append(message)
        return message

    async def edit_message(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        self.edits[message_id] = text

    async def delete_messages(self, chat_id: int, ids: list[int]) -> None:
        self.deleted.extend(ids)


class Event:
    """Just enough of a Telethon NewMessage or CallbackQuery event."""

    def __init__(
        self,
        client: FakeClient,
        sender: int,
        message: Any = None,
        data: str = "",
        message_id: int | None = None,
    ) -> None:
        self.sender_id = sender
        #: For a button: the message it is on.
        self.message_id = message_id
        self.message = message
        self.data = data.encode()
        self.raw_text = getattr(message, "raw_text", "")
        self.answers: list[str] = []
        self.edited: list[str] = []
        self._client = client

    async def reply(self, text: str, **kwargs: Any) -> Sent:
        return await self._client.send_message(self.sender_id, text, **kwargs)

    respond = reply

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit(self, text: str, **kwargs: Any) -> None:
        self.edited.append(text)


def document(name: str, size: int = 1000, mime: str = "video/x-matroska") -> Any:
    return SimpleNamespace(
        id=77,
        raw_text="",
        photo=None,
        sticker=None,
        voice=None,
        video_note=None,
        gif=None,
        video=None,
        document=SimpleNamespace(id=9000),
        file=SimpleNamespace(name=name, size=size, mime_type=mime, ext=".mkv"),
    )


def tap(bot: Bot, client: FakeClient, sender: int, data: str) -> Event:
    """A tap on a button of the menu or status message that `data` belongs to."""
    action, _, raw = data.partition(":")
    message_id = None
    if raw.isdigit():
        if action == "stop":
            job = bot._queue.find(int(raw))
            message_id = None if job is None else job.status_message_id
        elif (request := bot._requests.get(int(raw))) is not None:
            message_id = request.menu_message_id
    return Event(client, sender, data=data, message_id=message_id)


def text(value: str) -> Any:
    return SimpleNamespace(raw_text=value, photo=None, file=None, document=None)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def bot(client: FakeClient, config: Config, store: Store) -> Bot:
    return Bot(client, config, store, now=lambda: 0.0)


async def test_a_file_gets_a_menu(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    [menu] = client.sent
    assert "film.mkv" in menu.text
    labels = [button.text for row in menu.buttons for button in row]
    assert "▶️ Make playable" in labels


async def test_an_oversized_file_is_refused(bot: Bot, client: FakeClient, config: Config) -> None:
    await bot._on_message(Event(client, 42, document("huge.mkv", config.max_file_bytes + 1)))
    assert "The most I can take" in client.sent[0].text


async def test_rename_then_the_menu_is_shown_again(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    menu = client.sent[0]

    await bot._on_button(tap(bot, client, 42, "name:1"))
    prompt = client.sent[1]
    assert "new name" in prompt.text

    await bot._on_message(Event(client, 42, text("holiday")))
    assert menu.id in client.deleted
    assert prompt.id in client.deleted
    assert "holiday.mkv" in client.sent[-1].text


async def test_someone_else_cannot_use_your_menu(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    other = tap(bot, client, 99, "video:1")
    await bot._on_button(other)
    assert "expired" in other.answers[0]


async def test_choosing_an_action_queues_a_job(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "video:1"))
    assert bot._queue.waiting == 1
    assert bare(client.edits[client.sent[0].id]).startswith("⏳ Starting")


async def test_only_the_owner_can_cancel_a_job(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "video:1"))

    stranger = tap(bot, client, 99, "stop:1")
    await bot._on_button(stranger)
    assert bot._queue.waiting == 1

    await bot._on_button(tap(bot, client, 42, "stop:1"))
    assert bot._queue.waiting == 0


async def test_unanswered_menus_expire(client: FakeClient, config: Config, store: Store) -> None:
    clock = [0.0]
    bot = Bot(client, config, store, now=lambda: clock[0])
    await bot._on_message(Event(client, 42, document("film.mkv")))

    clock[0] = config.menu_timeout_s + 1
    await bot.sweep()
    assert "expired" in client.edits[client.sent[0].id]
    assert bot._requests == {}


def test_admins_get_their_own_limits(config: Config) -> None:
    assert config.limits_for(7) == config.admin_limits
    assert config.limits_for(42) == config.user_limits


async def test_a_file_over_the_daily_limit_gets_no_menu(
    bot: Bot, client: FakeClient, store: Store, config: Config
) -> None:
    for _ in range(config.user_limits.files_per_day):
        store.record(42, bot._calendar.today(), 1)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    assert "today's limit" in client.sent[0].text


async def test_queued_files_count_against_the_limit(
    client: FakeClient, store: Store, tmp_path: Path
) -> None:
    config = load(
        {
            "API_ID": "1",
            "API_HASH": "h",
            "BOT_TOKEN": "t",
            "DATA_DIR": str(tmp_path),
            "USER_DAILY_FILES": "1",
        }
    )
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("one.mkv")))
    await bot._on_message(Event(client, 42, document("two.mkv")))
    await bot._on_button(tap(bot, client, 42, "video:1"))

    second = tap(bot, client, 42, "video:2")
    await bot._on_button(second)
    assert "today's limit of 1 file." in bare(second.answers[0])


# ---------------------------------------------------------------- joining


class JoinClient(FakeClient):
    """Answers membership questions: members are in `members`."""

    def __init__(self, members: set[int]) -> None:
        super().__init__()
        self.members = members

    async def __call__(self, request: Any) -> Any:
        if request.participant not in self.members:
            raise UserNotParticipantError(request=None)
        return SimpleNamespace(participant=ChannelParticipant(user_id=1, date=None))


def joining_bot(client: FakeClient, store: Store, tmp_path: Path) -> Bot:
    config = load(
        {
            "API_ID": "1",
            "API_HASH": "h",
            "BOT_TOKEN": "t",
            "ADMIN_IDS": "7",
            "DATA_DIR": str(tmp_path),
            "JOIN_CHANNEL": "@newsroom",
        }
    )
    store.set_switch("join", True)
    return Bot(client, config, store, now=lambda: 0.0)


async def test_non_members_are_asked_to_join(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members=set())
    bot = joining_bot(client, store, tmp_path)
    await bot._on_message(Event(client, 42, document("film.mkv")))

    [reply] = client.sent
    assert "join our channel" in reply.text
    assert reply.buttons[0][0].type.url == "https://t.me/newsroom"
    assert bot._requests == {}


class SlowClient(FakeClient):
    """Telegram asking the bot to wait before it may check anyone."""

    async def __call__(self, request: Any) -> Any:
        raise FloodWaitError(request=None, capture=300)


async def test_a_failed_check_lets_nobody_in(store: Store, tmp_path: Path) -> None:
    client = SlowClient()
    bot = joining_bot(client, store, tmp_path)
    await bot._on_message(Event(client, 42, document("film.mkv")))

    [reply] = client.sent
    assert "couldn't check" in reply.text
    assert bot._requests == {}


async def test_members_and_admins_get_straight_in(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members={42})
    bot = joining_bot(client, store, tmp_path)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_message(Event(client, 7, document("film.mkv")))
    assert len(bot._requests) == 2


async def test_the_joined_button_checks_again(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members=set())
    bot = joining_bot(client, store, tmp_path)
    tapped = tap(bot, client, 42, "joined:0")
    await bot._on_button(tapped)
    assert "can't see you" in tapped.answers[0]


async def test_nobody_has_to_join_while_the_switch_is_off(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members=set())
    bot = joining_bot(client, store, tmp_path)
    store.set_switch("join", False)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    assert len(bot._requests) == 1


async def test_everyone_who_writes_is_remembered(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    await bot._on_anything(Event(client, 42, text("hi")))
    assert store.reachable_users() == [42]


# ---------------------------------------------------------------- keyboard


def keyboard_labels(message: Sent) -> list[str]:
    return [b.button.text for row in message.buttons for b in row]


async def test_start_shows_the_keyboard(bot: Bot, client: FakeClient, store: Store) -> None:
    store.set_language(42, "en")
    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 42, text("/start")))
    assert keyboard_labels(client.sent[0]) == ["❓ Help", "📊 My usage", "🌐 Language"]


async def test_admins_also_get_the_panel_button(bot: Bot, client: FakeClient, store: Store) -> None:
    store.set_language(7, "en")
    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 7, text("/start")))
    assert "🛠 Admin panel" in keyboard_labels(client.sent[0])


async def test_keyboard_buttons(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, text("📊 My usage")))
    assert bare(client.sent[-1].text).startswith("Today: 0 of 20 files")

    await bot._on_message(Event(client, 42, text("❓ Help")))
    assert "How it works" in client.sent[-1].text

    await bot._on_message(Event(client, 7, text("🛠 Admin panel")))
    assert "Admin panel" in client.sent[-1].text


async def test_the_panel_button_does_nothing_for_others(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, text("🛠 Admin panel")))
    assert "Admin panel" not in client.sent[-1].text


async def test_a_keyboard_tap_is_not_taken_as_a_new_name(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "name:1"))
    await bot._on_message(Event(client, 42, text("❓ Help")))
    assert bot._requests[1].new_name is None


# ---------------------------------------------------------------- thumbnails


class Picture:
    """A photo or image document sent in answer to the thumbnail prompt."""

    def __init__(self, size: int, source: Path | None = None) -> None:
        self.raw_text = ""
        self.photo = object()
        self.document = None
        self.file = SimpleNamespace(size=size, mime_type="image/jpeg")
        self.source = source
        self.downloads = 0

    async def download_media(self, file: str) -> str:
        self.downloads += 1
        if self.source is not None:
            Path(file).write_bytes(self.source.read_bytes())
        return file


async def ask_for_thumbnail(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "thumb:1"))


async def test_an_oversized_thumbnail_is_never_downloaded(bot: Bot, client: FakeClient) -> None:
    bot._thumbs.mkdir(parents=True)
    await ask_for_thumbnail(bot, client)
    picture = Picture(size=THUMBNAIL_MAX_BYTES + 1)

    await bot._on_message(Event(client, 42, picture))

    assert picture.downloads == 0
    assert "at most 10.0 MB" in client.sent[-1].text
    assert bot._requests[1].thumbnail is None


@needs_ffmpeg
async def test_a_thumbnail_is_set(
    bot: Bot, client: FakeClient, make_clip: MakeClip, tmp_path: Path
) -> None:
    bot._thumbs.mkdir(parents=True)
    frame = tmp_path / "frame.jpg"
    assert await media.frame_thumbnail(str(make_clip("x.mp4")), str(frame), 2.0)
    await ask_for_thumbnail(bot, client)

    await bot._on_message(Event(client, 42, Picture(frame.stat().st_size, frame)))

    thumbnail = bot._requests[1].thumbnail
    assert thumbnail is not None
    assert Path(thumbnail).exists()
    assert list(bot._thumbs.glob("*.upload")) == []


async def test_only_a_few_thumbnails_are_processed_at_once(
    bot: Bot, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot._thumbs.mkdir(parents=True)
    running, peak = 0, 0
    release = asyncio.Event()

    async def slow_fit(image: str, target: str) -> bool:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return False

    monkeypatch.setattr(media, "fit_thumbnail", slow_fit)
    tasks = []
    for user in range(100, 106):
        await bot._on_message(Event(client, user, document("film.mkv")))
        request_id = max(bot._requests)
        await bot._on_button(tap(bot, client, user, f"thumb:{request_id}"))
        tasks.append(asyncio.create_task(bot._on_message(Event(client, user, Picture(100)))))
    for _ in range(10):
        await asyncio.sleep(0)
    assert peak == THUMBNAIL_SLOTS

    release.set()
    await asyncio.gather(*tasks)


async def test_the_join_rule_is_checked_again_when_a_button_is_pressed(
    store: Store, tmp_path: Path
) -> None:
    client = JoinClient(members=set())
    bot = joining_bot(client, store, tmp_path)
    store.set_switch("join", False)
    await bot._on_message(Event(client, 42, document("film.mkv")))

    store.set_switch("join", True)
    tapped = tap(bot, client, 42, "video:1")
    await bot._on_button(tapped)

    assert "join our channel" in tapped.answers[0]
    assert bot._queue.waiting == 0


async def test_stop_waits_for_everything_it_started(bot: Bot) -> None:
    bot.start()
    await asyncio.sleep(0)
    await bot.stop()
    assert all(task.done() for task in bot._background)


async def test_thumbnail_attempts_are_rate_limited(
    bot: Bot, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot._thumbs.mkdir(parents=True)

    async def unreadable(image: str, target: str) -> bool:
        return False

    monkeypatch.setattr(media, "fit_thumbnail", unreadable)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    pictures = []
    for _ in range(THUMBNAIL_ATTEMPTS_PER_HOUR + 1):
        await bot._on_button(tap(bot, client, 42, "thumb:1"))
        pictures.append(Picture(100))
        await bot._on_message(Event(client, 42, pictures[-1]))

    assert [p.downloads for p in pictures] == [1] * THUMBNAIL_ATTEMPTS_PER_HOUR + [0]
    assert "a lot of thumbnails" in client.sent[-1].text


async def test_a_thumbnail_that_fails_halfway_leaves_nothing_behind(
    bot: Bot, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot._thumbs.mkdir(parents=True)

    async def breaks_after_writing(image: str, target: str) -> bool:
        Path(target).write_bytes(b"half a jpeg")
        raise RuntimeError("ffmpeg went away")

    monkeypatch.setattr(media, "fit_thumbnail", breaks_after_writing)
    await ask_for_thumbnail(bot, client)
    await bot._on_message(Event(client, 42, Picture(100)))

    assert list(bot._thumbs.iterdir()) == []
    assert bot._requests[1].thumbnail is None
    assert "couldn't use that picture" in client.sent[-1].text


async def test_startup_logs_a_channel_it_cannot_reach(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Fresh(FakeClient):
        async def get_input_entity(self, peer: Any) -> Any:
            raise ValueError("Could not find the input entity")

    config = load(
        {
            "API_ID": "1",
            "API_HASH": "h",
            "BOT_TOKEN": "t",
            "DATA_DIR": str(tmp_path),
            "CACHE_CHANNEL_ID": "-1004466589107",
        }
    )
    await Bot(Fresh(), config, store).check_channels()
    assert "cache channel -1004466589107" in caplog.text
    assert "post any message in the channel" in caplog.text


# ---------------------------------------------------------------- languages


def inline_labels(message: Sent) -> list[str]:
    return [button.text for row in message.buttons for button in row]


async def test_the_first_start_asks_for_a_language(bot: Bot, client: FakeClient) -> None:
    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 42, text("/start")))
    [picker] = client.sent
    assert inline_labels(picker) == ["English", "فارسی"]
    # Asked in every language, each line in its own direction.
    assert picker.text.splitlines() == [
        "\u200e🌐 Choose your language",
        "\u200f🌐 زبان خود را انتخاب کنید",
    ]


async def test_choosing_persian(bot: Bot, client: FakeClient, store: Store) -> None:
    choice = tap(bot, client, 42, "lang:fa")
    await bot._on_button(choice)

    assert store.language(42) == "fa"
    assert bare(choice.edited[0]) == "✅ از این به بعد ربات با شما فارسی صحبت می‌کند."
    welcome = client.sent[-1]
    assert "ویدیوی خود را به صورت فایل بفرستید" in welcome.text
    assert keyboard_labels(welcome) == ["❓ راهنما", "📊 میزان استفاده", "🌐 زبان"]

    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 42, text("/start")))
    assert "ویدیوی خود را" in client.sent[-1].text


async def test_the_language_button_marks_the_current_one(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    store.set_language(42, "fa")
    await bot._on_message(Event(client, 42, text("🌐 زبان")))
    styles = {b.text: b.style for row in client.sent[-1].buttons for b in row}
    assert styles["فارسی"].bg_success
    assert styles["English"].bg_primary


async def test_an_unknown_language_is_ignored(bot: Bot, client: FakeClient, store: Store) -> None:
    await bot._on_button(tap(bot, client, 42, "lang:xx"))
    assert store.language(42) is None
    assert client.sent == []


async def test_an_old_keyboard_still_works_after_switching(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    store.set_language(42, "fa")
    await bot._on_message(Event(client, 42, text("❓ Help")))
    reply = client.sent[-1]
    assert "روش کار" in reply.text
    # The answer brings the keyboard in the new language.
    assert keyboard_labels(reply)[0] == "❓ راهنما"


async def test_a_persian_menu_reads_right_to_left(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    store.set_language(42, "fa")
    await bot._on_message(Event(client, 42, document("holiday (2).mkv", size=1536 * 1024)))
    [menu] = client.sent
    lines = [line for line in menu.text.splitlines() if line]
    assert all(line.startswith("\u200f") for line in lines)
    # The English file name is isolated, so it can't reorder the sentence.
    assert "\u2068holiday (2).mkv\u2069" in lines[0]
    assert "۱٫۵ مگابایت" in lines[0]
    assert "▶️ قابل پخش کن" in inline_labels(menu)


async def test_alerts_are_translated(bot: Bot, client: FakeClient, store: Store) -> None:
    store.set_language(42, "fa")
    expired = tap(bot, client, 42, "video:99")
    await bot._on_button(expired)
    assert expired.answers == [FA.alert("menu.expired_alert")]


class Recorder:
    """A client that only records the requests it is asked to send."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def __call__(self, request: Any) -> None:
        self.requests.append(request)


async def test_commands_are_published_in_every_language(bot: Bot) -> None:
    recorder = Recorder()
    bot._client = recorder
    await bot.publish_commands()
    published = {r.lang_code: r.commands[0].description for r in recorder.requests}
    assert published == {"": "Show the menu", "en": "Show the menu", "fa": "نمایش منو"}


# ---------------------------------------------------------------- edits


def edited(document_id: int | None, message_id: int = 77) -> Any:
    document = None if document_id is None else SimpleNamespace(id=document_id)
    return SimpleNamespace(id=message_id, document=document)


async def test_replacing_the_file_closes_its_menu(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    menu = client.sent[0]

    await bot._on_edit(Event(client, 42, edited(document_id=1234)))
    assert bot._requests == {}
    assert "replaced the file" in client.edits[menu.id]

    stale = tap(bot, client, 42, "video:1")
    await bot._on_button(stale)
    assert bot._queue.waiting == 0


async def test_a_new_caption_keeps_the_menu(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_edit(Event(client, 42, edited(document_id=9000)))
    await bot._on_edit(Event(client, 99, edited(document_id=1234)))
    assert list(bot._requests) == [1]


async def test_the_sweep_forgets_old_thumbnail_attempts(
    client: FakeClient, config: Config, store: Store
) -> None:
    clock = [0.0]
    bot = Bot(client, config, store, now=lambda: clock[0])
    assert bot._may_try_thumbnail(42)
    clock[0] = 1800
    assert bot._may_try_thumbnail(43)
    clock[0] = 3601
    await bot.sweep()
    assert list(bot._thumbnail_attempts) == [43]


# ---------------------------------------------------------------- races


async def test_two_taps_at_once_queue_one_job(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    both_waiting = asyncio.Event()
    arrived = 0

    async def slow_check(user_id: int) -> bool:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_waiting.set()
        await both_waiting.wait()
        return True

    bot._is_allowed = slow_check  # type: ignore[method-assign]
    first, second = tap(bot, client, 42, "video:1"), tap(bot, client, 42, "video:1")
    await asyncio.gather(bot._on_button(first), bot._on_button(second))
    assert len(bot._queue.jobs_for(42)) == 1


async def test_a_refused_job_leaves_its_menu_open(
    client: FakeClient, store: Store, tmp_path: Path
) -> None:
    config = load({"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "t", "DATA_DIR": str(tmp_path)})
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    bot._queue._max_waiting = 0
    for _ in range(config.workers):
        filler = Job(id=bot._queue.next_id(), request=make_request(user_id=7), mode=Mode.FILE)
        await bot._queue.submit(filler)
    refused = tap(bot, client, 42, "video:1")
    await bot._on_button(refused)
    assert "busy" in refused.answers[0]
    assert list(bot._requests) == [1]


async def test_files_arriving_together_respect_the_menu_limit(config: Config, store: Store) -> None:
    class SlowReplies(FakeClient):
        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Sent:
            await asyncio.sleep(0)
            return await super().send_message(chat_id, text, **kwargs)

    client = SlowReplies()
    bot = Bot(client, config, store, now=lambda: 0.0)
    await asyncio.gather(
        *(bot._on_message(Event(client, 42, document("film.mkv"))) for _ in range(12))
    )
    assert len(bot._requests) == MAX_OPEN_MENUS


async def test_an_old_menu_does_nothing_after_a_restart(bot: Bot, client: FakeClient) -> None:
    # Request numbers start again at 1: the old menu's "file:1" names the new file.
    await bot._on_message(Event(client, 42, document("new-file.mkv")))
    old_menu = Event(client, 42, data="file:1", message_id=123)
    await bot._on_button(old_menu)
    assert bot._queue.jobs_for(42) == []
    assert "expired" in old_menu.answers[0]


async def test_an_old_cancel_button_does_nothing_after_a_restart(
    bot: Bot, client: FakeClient
) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "video:1"))
    await bot._on_button(Event(client, 42, data="stop:1", message_id=123))
    assert bot._queue.waiting == 1


class SlowDeletes(FakeClient):
    """Deleting a message waits until `go` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.deleting = asyncio.Event()
        self.go = asyncio.Event()

    async def delete_messages(self, chat_id: int, ids: list[int]) -> None:
        self.deleting.set()
        await self.go.wait()
        await super().delete_messages(chat_id, ids)


async def test_a_menu_being_renamed_waits_before_it_can_be_sent(
    config: Config, store: Store
) -> None:
    client = SlowDeletes()
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    client.go.set()
    await bot._on_button(tap(bot, client, 42, "name:1"))
    client.go.clear()
    client.deleting.clear()

    renaming = asyncio.create_task(bot._on_message(Event(client, 42, text("holiday"))))
    await client.deleting.wait()
    early = tap(bot, client, 42, "video:1")
    await bot._on_button(early)
    assert bot._queue.waiting == 0
    assert "One moment" in early.answers[0]

    client.go.set()
    await renaming
    await bot._on_button(tap(bot, client, 42, "video:1"))
    [job] = bot._queue.jobs_for(42)
    assert job.request.new_name == "holiday.mkv"


async def test_a_menu_cancelled_mid_rename_is_not_sent_again(config: Config, store: Store) -> None:
    client = SlowDeletes()
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    client.go.set()
    await bot._on_button(tap(bot, client, 42, "name:1"))
    client.go.clear()
    client.deleting.clear()
    sent_before = len(client.sent)

    renaming = asyncio.create_task(bot._on_message(Event(client, 42, text("holiday"))))
    await client.deleting.wait()
    await bot._on_button(tap(bot, client, 42, "drop:1"))
    client.go.set()
    await renaming

    assert bot._requests == {}
    assert len(client.sent) == sent_before


async def test_a_menu_cancelled_while_its_prompt_is_sent_leaves_no_prompt(
    config: Config, store: Store
) -> None:
    class SlowSends(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.sending = asyncio.Event()
            self.go = asyncio.Event()

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Sent:
            if "new name" in text:
                self.sending.set()
                await self.go.wait()
            return await super().send_message(chat_id, text, **kwargs)

    client = SlowSends()
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    asking = asyncio.create_task(bot._on_button(tap(bot, client, 42, "name:1")))
    await client.sending.wait()
    await bot._on_button(tap(bot, client, 42, "drop:1"))
    client.go.set()
    await asking

    assert bot._prompts == {}
    assert client.sent[-1].id in client.deleted


async def test_the_sweep_forgets_a_prompt_whose_menu_is_gone(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "name:1"))
    bot._requests.clear()
    await bot.sweep()
    assert bot._prompts == {}


def test_a_job_keeps_the_status_message_it_was_queued_with() -> None:
    request = make_request(menu_message_id=500)
    job = Job(id=1, request=request, mode=Mode.VIDEO)
    request.menu_message_id = 502
    assert job.status_message_id == 500


async def started_submission(
    bot: Bot, client: FakeClient
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """A tap on "Make playable" that is waiting on the join check."""
    checking, answer = asyncio.Event(), asyncio.Event()

    async def slow_check(user_id: int) -> bool:
        checking.set()
        await answer.wait()
        return True

    bot._is_allowed = slow_check  # type: ignore[method-assign]
    submitting = asyncio.create_task(bot._on_button(tap(bot, client, 42, "video:1")))
    await checking.wait()
    return submitting, answer


async def test_a_rename_started_during_the_join_check_holds_the_submission(
    config: Config, store: Store
) -> None:
    client = SlowDeletes()
    client.go.set()
    bot = Bot(client, config, store, now=lambda: 0.0)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "name:1"))
    client.go.clear()
    client.deleting.clear()

    submitting, answer = await started_submission(bot, client)
    renaming = asyncio.create_task(bot._on_message(Event(client, 42, text("holiday"))))
    await client.deleting.wait()
    answer.set()
    await submitting
    assert bot._queue.waiting == 0

    client.go.set()
    await renaming
    assert bot._requests[1].new_name == "holiday.mkv"


async def test_a_rename_finished_during_the_join_check_voids_the_old_tap(
    bot: Bot, client: FakeClient
) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(tap(bot, client, 42, "name:1"))

    submitting, answer = await started_submission(bot, client)
    await bot._on_message(Event(client, 42, text("holiday")))
    answer.set()
    await submitting
    # The tap was on the old menu, for the old name.
    assert bot._queue.waiting == 0
    assert list(bot._requests) == [1]


# ---------------------------------------------------------------- bans


async def test_a_banned_user_is_told_and_nothing_else_happens(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    store.ban(42)
    event = Event(client, 42, document("film.mkv"))
    event.is_private = True  # type: ignore[attr-defined]
    assert bot._is_banned_event(event)
    with pytest.raises(events.StopPropagation):
        await bot._on_banned(event)
    assert "banned" in bare(client.sent[0].text)

    button = Event(client, 42, data="video:1")
    await bot._on_button(button)
    assert "banned" in button.answers[0]


async def test_admins_are_never_treated_as_banned(bot: Bot, store: Store) -> None:
    store.ban(7)
    assert not bot._is_banned(7)


async def test_whoever_sent_a_file_is_remembered(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    assert store.senders(9000) == [42]


async def test_sending_a_banned_file_bans_the_sender(
    bot: Bot, client: FakeClient, store: Store
) -> None:
    store.ban_file(9000)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    assert store.is_banned(42)
    assert "banned" in bare(client.sent[0].text)
    assert bot._requests == {}


async def test_a_ban_closes_menus_and_stops_files(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_message(Event(client, 42, document("other.mkv")))
    await bot._on_button(tap(bot, client, 42, "video:1"))
    assert bot._queue.waiting == 1
    await bot._drop_user(42)
    assert bot._requests == {}
    assert bot._queue.waiting == 0
