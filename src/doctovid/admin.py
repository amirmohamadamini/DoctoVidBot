"""The admin panel: one message of buttons, edited in place."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import events

from .backup import Backups
from .broadcast import Broadcaster, Tally
from .config import Config
from .membership import POSTING_FIX, Membership, channel_problem
from .menus import KEYBOARD_LABELS
from .store import Store
from .transport import DANGER, PRIMARY, SUCCESS, Key, Keyboard, Style, to_buttons

log = logging.getLogger(__name__)

BROADCAST_PROMPT = (
    "Send me the message to broadcast: text, a photo, a video, a file, anything.\n"
    "Nothing is sent until you confirm."
)

#: Telegram cuts a button's pop-up answer off at 200 characters.
ALERT_LIMIT = 200


class Admin:
    def __init__(
        self,
        client: Any,
        config: Config,
        store: Store,
        *,
        broadcaster: Broadcaster,
        backups: Backups,
        membership: Membership | None,
        activity: Callable[[], str],
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._broadcaster = broadcaster
        self._backups = backups
        self._membership = membership
        self._activity = activity
        #: Admins the bot is waiting on for a message to broadcast.
        self._drafting: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self) -> None:
        """Must come before the bot's catch-all message handler."""
        self._client.add_event_handler(
            self._admins_only(self._on_draft),
            events.NewMessage(incoming=True, func=self._is_draft),
        )
        self._client.add_event_handler(
            self._admins_only(self._on_button),
            events.CallbackQuery(pattern=rb"^adm:"),
        )

    async def stop(self) -> None:
        """A broadcast in progress stops where it is."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._config.admin_ids

    def is_joining_required(self) -> bool:
        return self._membership is not None and self._store.switch("join")

    def cache_on(self) -> bool:
        return self._config.cache_channel_id is not None and self._store.cache_enabled()

    def _admins_only(
        self, handler: Callable[[Any], Awaitable[None]]
    ) -> Callable[[Any], Awaitable[None]]:
        async def guarded(event: Any) -> None:
            if not self.is_admin(event.sender_id):
                # A forwarded panel, or a forged callback: nothing happens.
                if isinstance(event, events.CallbackQuery.Event):
                    await event.answer()
                return
            await handler(event)
            raise events.StopPropagation

        return guarded

    # ------------------------------------------------------------ the panel

    async def open_panel(self, event: Any) -> None:
        text, keyboard = self.panel()
        await event.respond(text, buttons=to_buttons(keyboard))

    def panel(self) -> tuple[str, Keyboard]:
        users, active = self._store.user_counts()
        text = "\n".join(
            [
                "<b>Admin panel</b>",
                "",
                self._activity(),
                f"Users: {users}, {active} active this month",
                f"Cached files: {self._store.cache_size()}",
                f"Backups: {self._backups.describe()}",
            ]
        )
        cache, joining = self.cache_on(), self.is_joining_required()
        keyboard = [
            [Key(f"🗄 Cache: {_state(cache)}", "adm:cache", _switch_style(cache))],
            [Key(f"🔒 Join required: {_state(joining)}", "adm:join", _switch_style(joining))],
            [Key("📣 Broadcast", "adm:cast", PRIMARY), Key("💾 Backup now", "adm:backup")],
            [Key("🔄 Refresh", "adm:panel")],
        ]
        return text, keyboard

    async def _refresh(self, event: Any) -> None:
        text, keyboard = self.panel()
        # Refreshing a panel that hasn't changed is an error to Telegram.
        await _quietly(event.edit(text, buttons=to_buttons(keyboard)))

    async def _on_button(self, event: Any) -> None:
        action, _, argument = event.data.decode().removeprefix("adm:").partition(":")
        match action:
            case "panel":
                await event.answer()
                await self._refresh(event)
            case "cache":
                await self._toggle_cache(event)
            case "join":
                await self._toggle_join(event)
            case "backup":
                await self._backup(event)
            case "cast":
                self._drafting.add(event.sender_id)
                await event.answer()
                await event.respond(
                    BROADCAST_PROMPT,
                    buttons=to_buttons([[Key("✖️ Cancel", "adm:nocast", DANGER)]]),
                )
            case "send":
                await self._confirm_broadcast(event, int(argument))
            case "nocast":
                self._drafting.discard(event.sender_id)
                await event.edit("Broadcast cancelled.")
            case _:
                await event.answer()

    async def _toggle_cache(self, event: Any) -> None:
        channel = self._config.cache_channel_id
        if self.cache_on():
            self._store.set_cache_enabled(False)
            await event.answer("Cache is off. Files already in the channel stay there.")
        elif channel is None:
            await event.answer("Set CACHE_CHANNEL_ID first, then restart the bot.", alert=True)
            return
        elif problem := await self._check_posting(channel):
            await _alert(event, problem)
            return
        else:
            self._store.set_cache_enabled(True)
            await event.answer("Cache is on.")
        await self._refresh(event)

    async def _toggle_join(self, event: Any) -> None:
        if self.is_joining_required():
            self._store.set_switch("join", False)
            await event.answer("Users no longer have to join the channel.")
        elif self._membership is None:
            await event.answer("Set JOIN_CHANNEL first, then restart the bot.", alert=True)
            return
        elif problem := await self._membership.problem():
            await _alert(event, problem)
            return
        else:
            self._store.set_switch("join", True)
            await event.answer("Users now have to join the channel first.")
        await self._refresh(event)

    async def _backup(self, event: Any) -> None:
        await event.answer("Sending a backup…")
        delivered = await self._backups.send()
        await event.respond(f"Backup sent to {delivered} of {len(self._config.admin_ids)} admins.")

    async def _check_posting(self, channel: int) -> str | None:
        """There is no read-only way to learn whether a bot may post somewhere."""
        try:
            probe = await self._client.send_message(channel, "Checking I can post here.")
            await self._client.delete_messages(channel, [probe.id])
        except Exception as exc:
            return channel_problem(exc, fix=POSTING_FIX)
        return None

    # ------------------------------------------------------------ broadcast

    def _is_draft(self, event: Any) -> bool:
        # A tap on the keyboard is the admin doing something else, not the
        # message to send.
        text = event.raw_text or ""
        return (
            bool(event.is_private)
            and event.sender_id in self._drafting
            and text not in KEYBOARD_LABELS
            and not text.startswith("/")
        )

    async def _on_draft(self, event: Any) -> None:
        self._drafting.discard(event.sender_id)
        if self._broadcaster.running:
            await event.reply("A broadcast is already running.")
            return
        count = len(self._store.reachable_users())
        buttons = [
            [
                Key("✅ Send", f"adm:send:{event.message.id}", SUCCESS),
                Key("✖️ Cancel", "adm:nocast", DANGER),
            ]
        ]
        await event.reply(f"Send this message to {count} people?", buttons=to_buttons(buttons))

    async def _confirm_broadcast(self, event: Any, message_id: int) -> None:
        if self._broadcaster.running:
            await event.answer("A broadcast is already running.", alert=True)
            return
        message = await self._client.get_messages(event.chat_id, ids=message_id)
        if message is None:
            await event.edit("That message has been deleted.")
            return
        await event.edit("Broadcasting…")
        task = asyncio.create_task(self._broadcast(event, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _broadcast(self, event: Any, message: Any) -> None:
        async def report(tally: Tally) -> None:
            await _quietly(event.edit(f"Broadcasting… {tally.describe()}"))

        try:
            tally = await self._broadcaster.send(message, report)
        except Exception:
            log.exception("broadcast failed")
            await _quietly(event.edit("The broadcast failed. See the logs."))
            return
        await _quietly(event.edit(f"Broadcast finished. {tally.describe()}."))


def _state(on: bool) -> str:
    return "on" if on else "off"


def _switch_style(on: bool) -> Style | None:
    """A switch that is on is green; one that is off looks like any button."""
    return SUCCESS if on else None


async def _alert(event: Any, text: str) -> None:
    await event.answer(text[:ALERT_LIMIT], alert=True)


async def _quietly(edit: Awaitable[Any]) -> None:
    try:
        await edit
    except Exception:
        log.debug("admin message edit failed", exc_info=True)
