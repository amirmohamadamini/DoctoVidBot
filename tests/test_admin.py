from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import events

from doctovid.admin import Admin
from doctovid.backup import Backups
from doctovid.broadcast import Broadcaster
from doctovid.config import Config
from doctovid.i18n import Html
from doctovid.menus import keyboard_actions
from doctovid.store import Store

from .conftest import EN, FA, LOCALES, bare


class Chat:
    """An admin's chat with the bot, as one event: a message or a button tap."""

    def __init__(self, sender: int, *, text: str = "", data: str = "", message_id: int = 0):
        self.sender_id = sender
        self.chat_id = sender
        self.is_private = True
        self.raw_text = text
        self.data = data.encode()
        self.message = SimpleNamespace(id=message_id)
        self.said: list[str] = []
        self.answers: list[str] = []
        self.edited: list[str] = []
        self.buttons: Any = None

    async def respond(self, text: str, buttons: Any = None) -> None:
        self.said.append(text)
        self.buttons = buttons

    reply = respond

    async def edit(self, text: str, buttons: Any = None) -> None:
        self.edited.append(text)
        self.buttons = buttons

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)


class FakeClient:
    def __init__(self) -> None:
        self.copied: list[int] = []
        self.files: list[int] = []

    async def get_messages(self, chat_id: int, ids: int) -> Any:
        return SimpleNamespace(id=ids)

    async def send_message(self, chat_id: int, message: Any) -> Any:
        self.copied.append(chat_id)
        return SimpleNamespace(id=1)

    async def delete_messages(self, chat_id: int, ids: list[int]) -> None:
        pass

    async def send_file(self, chat_id: int, path: str, **kwargs: Any) -> None:
        self.files.append(chat_id)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def build_admin(client: FakeClient, config: Config, store: Store, tmp_path: Path) -> Admin:
    async def no_sleep(seconds: float) -> None:
        pass

    return Admin(
        client,
        config,
        store,
        broadcaster=Broadcaster(client, store, sleep=no_sleep),
        backups=Backups(
            client,
            store,
            admins=config.admin_ids,
            interval_hours=12,
            directory=tmp_path,
            locale_for=lambda _admin: EN,
        ),
        membership=None,
        activity=lambda t: Html("Running: 0 of 2"),
        locale_for=lambda user_id: LOCALES.get(store.language(user_id)),
        is_keyboard_label=keyboard_actions(LOCALES).__contains__,
    )


@pytest.fixture
def admin(client: FakeClient, config: Config, store: Store, tmp_path: Path) -> Admin:
    return build_admin(client, config, store, tmp_path)


async def tap(admin: Admin, data: str, sender: int = 7) -> Chat:
    event = Chat(sender, data=data)
    with pytest.raises(events.StopPropagation):
        await admin._admins_only(admin._on_button)(event)
    return event


def labels(buttons: Any) -> list[str]:
    return [button.text for row in buttons for button in row]


def test_the_panel_shows_the_state_of_things(admin: Admin, store: Store) -> None:
    store.seen(42)
    text, keyboard = admin.panel(EN)
    assert "Users: 1, 1 active this month" in bare(text)
    assert "Backups: every 12 h, never delivered" in bare(text)
    assert [key.label for row in keyboard for key in row] == [
        "🗄 Cache: off",
        "🔒 Join required: off",
        "📣 Broadcast",
        "💾 Backup now",
        "🔄 Refresh",
    ]


async def test_other_people_cannot_press_admin_buttons(admin: Admin, store: Store) -> None:
    event = Chat(42, data="adm:cache")
    await admin._admins_only(admin._on_button)(event)
    assert not store.cache_enabled()


async def test_the_cache_button_toggles_after_checking_the_channel(
    admin: Admin, store: Store, client: FakeClient, config: Config
) -> None:
    turned_on = await tap(admin, "adm:cache")
    assert store.cache_enabled()
    assert config.cache_channel_id in client.copied
    assert "🗄 Cache: on" in labels(turned_on.buttons)
    assert turned_on.buttons[0][0].style.bg_success

    await tap(admin, "adm:cache")
    assert not store.cache_enabled()


async def test_join_needs_a_channel_first(admin: Admin, store: Store) -> None:
    event = await tap(admin, "adm:join")
    assert "Set JOIN_CHANNEL first" in event.answers[0]
    assert not store.switch("join")


async def test_backup_button(admin: Admin, client: FakeClient) -> None:
    event = await tap(admin, "adm:backup")
    assert client.files == [7]
    assert [bare(text) for text in event.said] == ["Backup sent to 1 of 1 admin."]


async def test_broadcast_asks_for_the_message_then_confirms(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    for user_id in (1, 2, 3):
        store.seen(user_id)

    await tap(admin, "adm:cast")
    assert not admin._is_draft(Chat(7, text=EN.label("keyboard.help")))
    assert not admin._is_draft(Chat(7, text=FA.label("keyboard.help")))
    assert not admin._is_draft(Chat(42, text="hello"))

    draft = Chat(7, text="Big news", message_id=55)
    assert admin._is_draft(draft)
    with pytest.raises(events.StopPropagation):
        await admin._admins_only(admin._on_draft)(draft)
    assert [bare(text) for text in draft.said] == ["Send this message to 3 people?"]
    assert draft.buttons[0][0].type.data == b"adm:send:55"

    confirm = await tap(admin, "adm:send:55")
    for task in list(admin._tasks):
        await task
    assert client.copied == [1, 2, 3]
    assert (
        bare(confirm.edited[-1]) == "Broadcast finished. 3 of 3: 3 sent, 0 unreachable, 0 failed."
    )


async def test_a_broadcast_can_be_cancelled(admin: Admin) -> None:
    await tap(admin, "adm:cast")
    await tap(admin, "adm:nocast")
    assert not admin._is_draft(Chat(7, text="never mind"))


class UnseenChannelClient(FakeClient):
    """A fresh session that has never seen the cache channel."""

    async def send_message(self, chat_id: int, message: Any) -> Any:
        raise ValueError("Could not find the input entity for PeerChannel(channel_id=4466589107)")


async def test_an_unseen_channel_gets_advice_not_telethons_error(
    config: Config, store: Store, tmp_path: Path
) -> None:
    admin = build_admin(UnseenChannelClient(), config, store, tmp_path)
    event = await tap(admin, "adm:cache")

    assert "post any message in the channel" in event.answers[0]
    assert "input entity" not in event.answers[0]
    assert len(event.answers[0]) <= 200
    assert not store.cache_enabled()


def test_the_panel_speaks_the_admins_language(admin: Admin, store: Store) -> None:
    store.set_language(7, "fa")
    text, keyboard = admin.panel(FA)
    assert "پنل مدیریت" in text
    assert "🗄 کش: خاموش" in [key.label for row in keyboard for key in row]


async def test_answers_follow_the_admins_language(admin: Admin, store: Store) -> None:
    store.set_language(7, "fa")
    event = await tap(admin, "adm:join")
    assert "JOIN_CHANNEL" in event.answers[0]
    assert event.answers[0] == FA.alert("admin.join_unset")
