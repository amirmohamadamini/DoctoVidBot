from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import events
from telethon.tl.types import MessageFwdHeader, PeerChannel

from doctovid.admin import Admin
from doctovid.backup import Backups
from doctovid.broadcast import Broadcaster
from doctovid.config import Config
from doctovid.i18n import Html, plain
from doctovid.menus import keyboard_actions
from doctovid.store import Store

from .conftest import EN, FA, LOCALES, bare


class Chat:
    """An admin's chat with the bot, as one event: a message or a button tap."""

    def __init__(
        self,
        sender: int,
        *,
        text: str = "",
        data: str = "",
        message_id: int = 0,
        forwarded: bool = False,
    ):
        self.sender_id = sender
        self.chat_id = sender
        self.is_private = True
        self.raw_text = text
        self.data = data.encode()
        self.message = SimpleNamespace(id=message_id, fwd_from=object() if forwarded else None)
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
        self.forwarded: list[int] = []
        self.files: list[int] = []
        #: Messages in the admin's chat that were forwarded from somewhere.
        self.forwards: set[int] = set()
        #: Set, fetching a message waits for it.
        self.hold: asyncio.Event | None = None
        self.fetch_error: Exception | None = None
        self.handlers: list[Any] = []

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append(callback)

    async def get_messages(self, chat_id: int, ids: int) -> Any:
        if self.hold is not None:
            await self.hold.wait()
        if self.fetch_error is not None:
            raise self.fetch_error
        return SimpleNamespace(id=ids, fwd_from=object() if ids in self.forwards else None)

    async def forward_messages(self, chat_id: int, message: Any) -> Any:
        self.forwarded.append(chat_id)
        return [SimpleNamespace(id=1)]

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
        "🚫 Ban",
        "✅ Unban",
        "🔄 Refresh",
    ]


async def test_other_people_cannot_press_admin_buttons(admin: Admin, store: Store) -> None:
    event = Chat(42, data="adm:cache")
    await admin._admins_only(admin._on_button)(event)
    assert not store.cache_enabled()


async def test_every_registered_handler_turns_away_other_people(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    admin.register()
    assert len(client.handlers) == 2
    admin._drafting.add(42)
    for handler in client.handlers:
        event = Chat(42, text="hello", data="adm:cache", message_id=55)
        await handler(event)
        assert (event.said, event.edited) == ([], [])
    assert not store.cache_enabled()
    assert admin._drafts == {}


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


async def draft(admin: Admin, message_id: int, *, forwarded: bool = False) -> Chat:
    await tap(admin, "adm:cast")
    event = Chat(7, text="news", message_id=message_id, forwarded=forwarded)
    with pytest.raises(events.StopPropagation):
        await admin._admins_only(admin._on_draft)(event)
    return event


async def finish(admin: Admin) -> None:
    for task in list(admin._tasks):
        await task


async def test_only_the_drafted_message_can_be_broadcast(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    # Forged confirms: with no draft at all, and naming some other message.
    await tap(admin, "adm:send:12")
    await tap(admin, "adm:send")
    assert client.copied == []

    await draft(admin, 55)
    await tap(admin, "adm:send:12")
    assert client.copied == []
    await tap(admin, "adm:send:55")
    await finish(admin)
    assert client.copied == [1]

    # Sending it once used the draft up.
    await tap(admin, "adm:send:55")
    assert client.copied == [1]


async def test_an_older_confirm_cannot_send_a_newer_draft(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    await draft(admin, 55)
    await draft(admin, 56)
    stale = await tap(admin, "adm:send:55")
    assert "replaced" in bare(stale.edited[-1])
    assert client.copied == []

    await tap(admin, "adm:send:56")
    await finish(admin)
    assert client.copied == [1]


async def test_a_forwarded_draft_can_be_forwarded_or_copied(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    client.forwards.add(55)
    event = await draft(admin, 55, forwarded=True)
    assert "Forwarded from" in bare(event.said[-1])
    assert [[button.type.data for button in row] for row in event.buttons] == [
        [b"adm:send:55", b"adm:fwd:55"],
        [b"adm:nocast"],
    ]
    await tap(admin, "adm:fwd:55")
    await finish(admin)
    assert (client.forwarded, client.copied) == ([1], [])

    client.forwards.add(56)
    await draft(admin, 56, forwarded=True)
    await tap(admin, "adm:send:56")
    await finish(admin)
    assert (client.forwarded, client.copied) == ([1], [1])


async def test_the_admins_own_message_is_never_forwarded(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    event = await draft(admin, 55)
    assert [[button.type.data for button in row] for row in event.buttons] == [
        [b"adm:send:55", b"adm:nocast"]
    ]
    # A forged forward of a message the admin wrote is sent as a copy, so it
    # doesn't show everyone who wrote it.
    await tap(admin, "adm:fwd:55")
    await finish(admin)
    assert (client.forwarded, client.copied) == ([], [1])


async def test_a_forward_confirm_is_checked_like_any_other(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    client.forwards.update({55, 56})
    await draft(admin, 55, forwarded=True)
    await draft(admin, 56, forwarded=True)
    await tap(admin, "adm:fwd:55")
    await tap(admin, "adm:fwd")
    assert client.forwarded == []


async def test_two_confirms_at_once_send_once_and_lose_nothing(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    await draft(admin, 55)
    client.hold = asyncio.Event()
    first = asyncio.create_task(tap(admin, "adm:send:55"))
    await asyncio.sleep(0)
    second = await tap(admin, "adm:send:55")
    assert second.answers == [EN.alert("broadcast.busy")]
    assert second.edited == []

    client.hold.set()
    await first
    await finish(admin)
    assert client.copied == [1]
    assert not admin._broadcaster.running


async def test_a_failed_confirm_keeps_the_draft(
    admin: Admin, store: Store, client: FakeClient
) -> None:
    store.seen(1)
    await draft(admin, 55)
    client.fetch_error = ConnectionError("network down")
    with pytest.raises(ConnectionError):
        await admin._on_button(Chat(7, data="adm:send:55"))
    assert not admin._broadcaster.running

    client.fetch_error = None
    await tap(admin, "adm:send:55")
    await finish(admin)
    assert client.copied == [1]


async def test_malformed_button_data_is_harmless(admin: Admin) -> None:
    event = Chat(7)
    event.data = b"adm:\xff\xfe"
    with pytest.raises(events.StopPropagation):
        await admin._admins_only(admin._on_button)(event)
    assert event.answers == [""]


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


def test_the_panel_warns_when_the_join_check_lets_everyone_in(
    client: FakeClient, config: Config, store: Store, tmp_path: Path
) -> None:
    admin = build_admin(client, config, store, tmp_path)
    admin._membership = SimpleNamespace(broken=True)  # type: ignore[assignment]
    store.set_switch("join", True)
    text, _ = admin.panel(EN)
    assert "everyone is let in" in text


# ---------------------------------------------------------------- bans


async def answer(admin: Admin, event: Chat) -> Chat:
    with pytest.raises(events.StopPropagation):
        await admin._admins_only(admin._on_draft)(event)
    return event


def sent_file(message_id: int, document_id: int, forward: Any = None) -> Chat:
    event = Chat(7, message_id=message_id)
    event.message = SimpleNamespace(
        id=message_id, fwd_from=forward, document=SimpleNamespace(id=document_id)
    )
    return event


def confirm_data(event: Chat) -> str:
    data: bytes = event.buttons[0][0].type.data
    return data.decode()


async def test_a_user_is_banned_by_id_after_confirming(admin: Admin, store: Store) -> None:
    banned: list[int] = []

    async def on_ban(user_id: int) -> None:
        banned.append(user_id)

    admin._on_ban = on_ban
    prompt = await tap(admin, "adm:bans")
    assert "Send me the id" in bare(plain(prompt.said[0]))
    question = await answer(admin, Chat(7, text=" 42 ", message_id=60))
    assert "Ban 42?" in bare(plain(question.said[0]))
    assert not store.is_banned(42)

    done = await tap(admin, confirm_data(question))
    assert store.is_banned(42)
    assert banned == [42]
    assert bare(plain(done.edited[0])) == "Banned 42."
    assert "Banned: 1 person" in bare(admin.panel(EN)[0])
    assert store.reachable_users() == []


async def test_admins_cannot_be_banned(admin: Admin, store: Store) -> None:
    await tap(admin, "adm:bans")
    event = await answer(admin, Chat(7, text="7", message_id=60))
    assert "Admins can't be banned" in bare(plain(event.said[0]))
    assert not store.is_banned(7)


async def test_something_unclear_is_asked_for_again(admin: Admin) -> None:
    await tap(admin, "adm:bans")
    event = await answer(admin, Chat(7, text="spammer", message_id=60))
    assert "isn't a user id or a file" in bare(plain(event.said[0]))
    assert admin._asking == {7: "ban"}


async def test_everyone_who_sent_a_file_is_banned_with_it(admin: Admin, store: Store) -> None:
    store.sent(9000, 42)
    store.sent(9000, 43)
    store.sent(9000, 7)  # an admin, left alone
    store.sent(9001, 44)
    await tap(admin, "adm:bans")
    question = await answer(admin, sent_file(60, 9000))
    assert "2 people sent this file: 42, 43" in bare(plain(question.said[0]))

    await tap(admin, confirm_data(question))
    assert [store.is_banned(user) for user in (42, 43, 7, 44)] == [True, True, False, False]
    assert store.is_file_banned(9000)


async def test_a_file_forwarded_from_the_cache_channel_leads_to_its_senders(
    client: FakeClient, config: Config, store: Store, tmp_path: Path
) -> None:
    config = dataclasses.replace(config, cache_channel_id=-1001234567890)
    admin = build_admin(client, config, store, tmp_path)
    store.remember(-1001234567890, "9000:video", 321)
    store.sent(9000, 42)
    forward = MessageFwdHeader(date=None, from_id=PeerChannel(1234567890), channel_post=321)
    await tap(admin, "adm:bans")
    # The copy in the channel is a file of its own, with another id.
    question = await answer(admin, sent_file(60, 5555, forward))
    assert "1 person sent this file: 42" in bare(plain(question.said[0]))
    await tap(admin, confirm_data(question))
    assert store.is_banned(42)
    assert store.is_file_banned(9000)


async def test_only_the_latest_ban_can_be_confirmed(admin: Admin, store: Store) -> None:
    await tap(admin, "adm:bans")
    first = await answer(admin, Chat(7, text="42", message_id=60))
    await tap(admin, "adm:bans")
    await answer(admin, Chat(7, text="43", message_id=61))
    stale = await tap(admin, confirm_data(first))
    assert "already done, cancelled or replaced" in bare(plain(stale.edited[0]))
    assert not store.is_banned(42)


async def test_a_ban_can_be_cancelled(admin: Admin, store: Store) -> None:
    await tap(admin, "adm:bans")
    question = await answer(admin, Chat(7, text="42", message_id=60))
    await tap(admin, "adm:noban")
    await tap(admin, confirm_data(question))
    assert not store.is_banned(42)


async def test_unbanning_a_user_or_a_file(admin: Admin, store: Store) -> None:
    store.ban(42)
    store.ban_file(9000)
    store.ban(43, 9000)
    await tap(admin, "adm:unbans")
    event = await answer(admin, Chat(7, text="42", message_id=60))
    assert bare(plain(event.said[0])) == "Unbanned 42."
    assert not store.is_banned(42)

    await tap(admin, "adm:unbans")
    event = await answer(admin, sent_file(61, 9000))
    assert "File unbanned, and 1 person" in bare(plain(event.said[0]))
    assert not store.is_banned(43)
    assert not store.is_file_banned(9000)
