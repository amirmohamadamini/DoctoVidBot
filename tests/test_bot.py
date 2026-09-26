from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import events
from telethon.errors import UserNotParticipantError
from telethon.tl.types import ChannelParticipant

from doctovid import media
from doctovid.bot import (
    THUMBNAIL_ATTEMPTS_PER_HOUR,
    THUMBNAIL_MAX_BYTES,
    THUMBNAIL_SLOTS,
    Bot,
)
from doctovid.config import Config, load
from doctovid.store import Store

from .conftest import MakeClip, needs_ffmpeg


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
        self, client: FakeClient, sender: int, message: Any = None, data: str = ""
    ) -> None:
        self.sender_id = sender
        self.message = message
        self.data = data.encode()
        self.raw_text = getattr(message, "raw_text", "")
        self.answers: list[str] = []
        self._client = client

    async def reply(self, text: str, **kwargs: Any) -> Sent:
        return await self._client.send_message(self.sender_id, text, **kwargs)

    respond = reply

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)


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

    await bot._on_button(Event(client, 42, data="name:1"))
    prompt = client.sent[1]
    assert "new name" in prompt.text

    await bot._on_message(Event(client, 42, text("holiday")))
    assert menu.id in client.deleted
    assert prompt.id in client.deleted
    assert "holiday.mkv" in client.sent[-1].text


async def test_someone_else_cannot_use_your_menu(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    other = Event(client, 99, data="video:1")
    await bot._on_button(other)
    assert "expired" in other.answers[0]


async def test_choosing_an_action_queues_a_job(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(Event(client, 42, data="video:1"))
    assert bot._queue.waiting == 1
    assert client.edits[client.sent[0].id].startswith("⏳ Starting")


async def test_only_the_owner_can_cancel_a_job(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(Event(client, 42, data="video:1"))

    stranger = Event(client, 99, data="stop:1")
    await bot._on_button(stranger)
    assert bot._queue.waiting == 1

    await bot._on_button(Event(client, 42, data="stop:1"))
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
    await bot._on_button(Event(client, 42, data="video:1"))

    second = Event(client, 42, data="video:2")
    await bot._on_button(second)
    assert "today's limit of 1 files" in second.answers[0]


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


async def test_members_and_admins_get_straight_in(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members={42})
    bot = joining_bot(client, store, tmp_path)
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_message(Event(client, 7, document("film.mkv")))
    assert len(bot._requests) == 2


async def test_the_joined_button_checks_again(store: Store, tmp_path: Path) -> None:
    client = JoinClient(members=set())
    bot = joining_bot(client, store, tmp_path)
    tapped = Event(client, 42, data="joined:0")
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


async def test_start_shows_the_keyboard(bot: Bot, client: FakeClient) -> None:
    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 42, text("/start")))
    assert [b.button.text for row in client.sent[0].buttons for b in row] == [
        "❓ Help",
        "📊 My usage",
    ]


async def test_admins_also_get_the_panel_button(bot: Bot, client: FakeClient) -> None:
    with pytest.raises(events.StopPropagation):
        await bot._on_start(Event(client, 7, text("/start")))
    assert "🛠 Admin panel" in [b.button.text for row in client.sent[0].buttons for b in row]


async def test_keyboard_buttons(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, text("📊 My usage")))
    assert client.sent[-1].text.startswith("Today: 0 of 20 files")

    await bot._on_message(Event(client, 42, text("❓ Help")))
    assert "How it works" in client.sent[-1].text

    await bot._on_message(Event(client, 7, text("🛠 Admin panel")))
    assert "Admin panel" in client.sent[-1].text


async def test_the_panel_button_does_nothing_for_others(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, text("🛠 Admin panel")))
    assert "Admin panel" not in client.sent[-1].text


async def test_a_keyboard_tap_is_not_taken_as_a_new_name(bot: Bot, client: FakeClient) -> None:
    await bot._on_message(Event(client, 42, document("film.mkv")))
    await bot._on_button(Event(client, 42, data="name:1"))
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
    await bot._on_button(Event(client, 42, data="thumb:1"))


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
        await bot._on_button(Event(client, user, data=f"thumb:{request_id}"))
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
    tapped = Event(client, 42, data="video:1")
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
        await bot._on_button(Event(client, 42, data="thumb:1"))
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
