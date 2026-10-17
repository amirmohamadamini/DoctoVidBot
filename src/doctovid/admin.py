"""The admin panel: one message of buttons, edited in place."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import events

from .backup import Backups
from .broadcast import Broadcaster, Refetch, Tally
from .config import Config
from .i18n import Html, Locale, plain
from .membership import POSTING_FIX, Membership, channel_problem
from .store import Store
from .transport import DANGER, PRIMARY, SUCCESS, Key, Keyboard, Style, to_buttons

log = logging.getLogger(__name__)

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
        activity: Callable[[Locale], Html],
        locale_for: Callable[[int], Locale],
        is_keyboard_label: Callable[[str], bool],
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._broadcaster = broadcaster
        self._backups = backups
        self._membership = membership
        self._activity = activity
        self._locale_for = locale_for
        self._is_keyboard_label = is_keyboard_label
        #: Admins the bot is waiting on for a message to broadcast.
        self._drafting: set[int] = set()
        #: The message each admin drafted, waiting for them to confirm it. A
        #: confirm button names its draft, and only counts while that draft is
        #: the admin's latest: an older button can't send a newer message.
        self._drafts: dict[int, int] = {}
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
        text, keyboard = self.panel(self._t(event))
        await event.respond(text, buttons=to_buttons(keyboard))

    def panel(self, t: Locale) -> tuple[Html, Keyboard]:
        users, active = self._store.user_counts()
        text = t.lines(
            t.phrase("admin.title"),
            Html(""),
            self._activity(t),
            t.phrase("admin.users", count=users, active=active),
            t.phrase("admin.cached", count=self._store.cache_size(self._config.cache_channel_id)),
            t.phrase("admin.backups", state=self._backups.describe(t)),
        )
        if self.is_joining_required() and self._membership is not None and self._membership.broken:
            text = t.lines(text, t.phrase("admin.join_broken"))
        cache, joining = self.cache_on(), self.is_joining_required()
        keyboard = [
            [
                Key(
                    t.label("admin.cache", state=_state(cache, t)),
                    "adm:cache",
                    _switch_style(cache),
                )
            ],
            [
                Key(
                    t.label("admin.join", state=_state(joining, t)),
                    "adm:join",
                    _switch_style(joining),
                )
            ],
            [
                Key(t.label("admin.broadcast"), "adm:cast", PRIMARY),
                Key(t.label("admin.backup_now"), "adm:backup"),
            ],
            [Key(t.label("admin.refresh"), "adm:panel")],
        ]
        return text, keyboard

    def _t(self, event: Any) -> Locale:
        return self._locale_for(event.sender_id)

    async def _refresh(self, event: Any) -> None:
        text, keyboard = self.panel(self._t(event))
        # Refreshing a panel that hasn't changed is an error to Telegram.
        await _quietly(event.edit(text, buttons=to_buttons(keyboard)))

    async def _on_button(self, event: Any) -> None:
        action, _, argument = (
            event.data.decode(errors="replace").removeprefix("adm:").partition(":")
        )
        t = self._t(event)
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
                    t.text("broadcast.prompt"),
                    buttons=to_buttons([[Key(t.label("menu.cancel"), "adm:nocast", DANGER)]]),
                )
            case "send" | "fwd":
                await self._confirm_broadcast(event, argument, forward=action == "fwd")
            case "nocast":
                self._drafting.discard(event.sender_id)
                self._drafts.pop(event.sender_id, None)
                await event.edit(t.text("broadcast.cancelled"))
            case _:
                await event.answer()

    async def _toggle_cache(self, event: Any) -> None:
        t = self._t(event)
        channel = self._config.cache_channel_id
        if self.cache_on():
            self._store.set_cache_enabled(False)
            await event.answer(t.alert("admin.cache_off"))
        elif channel is None:
            await event.answer(t.alert("admin.cache_unset"), alert=True)
            return
        elif problem := await self._check_posting(channel, t):
            await _alert(event, t, problem)
            return
        else:
            self._store.set_cache_enabled(True)
            await event.answer(t.alert("admin.cache_on"))
        await self._refresh(event)

    async def _toggle_join(self, event: Any) -> None:
        t = self._t(event)
        if self.is_joining_required():
            self._store.set_switch("join", False)
            await event.answer(t.alert("admin.join_off"))
        elif self._membership is None:
            await event.answer(t.alert("admin.join_unset"), alert=True)
            return
        elif problem := await self._membership.problem(t):
            await _alert(event, t, problem)
            return
        else:
            self._store.set_switch("join", True)
            await event.answer(t.alert("admin.join_on"))
        await self._refresh(event)

    async def _backup(self, event: Any) -> None:
        t = self._t(event)
        await event.answer(t.alert("admin.backup_sending"))
        delivered = await self._backups.send()
        admins = len(self._config.admin_ids)
        await event.respond(t.text("admin.backup_sent", sent=delivered, count=admins))

    async def _check_posting(self, channel: int, t: Locale) -> Html | None:
        """There is no read-only way to learn whether a bot may post somewhere."""
        try:
            probe = await self._client.send_message(channel, t.text("admin.post_check"))
            await self._client.delete_messages(channel, [probe.id])
        except Exception as exc:
            return channel_problem(exc, fix=POSTING_FIX, t=t)
        return None

    # ------------------------------------------------------------ broadcast

    def _is_draft(self, event: Any) -> bool:
        # A tap on the keyboard is the admin doing something else, not the
        # message to send.
        text = event.raw_text or ""
        return (
            bool(event.is_private)
            and event.sender_id in self._drafting
            and not self._is_keyboard_label(text)
            and not text.startswith("/")
        )

    async def _on_draft(self, event: Any) -> None:
        self._drafting.discard(event.sender_id)
        t = self._t(event)
        if self._broadcaster.running:
            await event.reply(t.text("broadcast.busy"))
            return
        message_id = event.message.id
        self._drafts[event.sender_id] = message_id
        count = len(self._store.reachable_users())
        parts = [t.phrase("broadcast.confirm", count=count)]
        cancel = Key(t.label("menu.cancel"), "adm:nocast", DANGER)
        if _forwarded(event.message):
            parts += [Html(""), t.phrase("broadcast.forward_hint")]
            buttons = [
                [
                    Key(t.label("broadcast.copy"), f"adm:send:{message_id}", SUCCESS),
                    Key(t.label("broadcast.forward"), f"adm:fwd:{message_id}", SUCCESS),
                ],
                [cancel],
            ]
        else:
            buttons = [[Key(t.label("broadcast.send"), f"adm:send:{message_id}", SUCCESS), cancel]]
        await event.reply(t.lines(*parts), buttons=to_buttons(buttons))

    async def _confirm_broadcast(self, event: Any, draft: str, *, forward: bool) -> None:
        t = self._t(event)
        if not self._broadcaster.claim():
            await event.answer(t.alert("broadcast.busy"), alert=True)
            return
        started = False
        try:
            started = await self._start_broadcast(event, t, draft, forward)
        finally:
            if not started:
                self._broadcaster.release()

    async def _start_broadcast(self, event: Any, t: Locale, draft: str, forward: bool) -> bool:
        """Under the broadcast slot. True once the broadcast task owns it."""
        message_id = self._drafts.get(event.sender_id)
        if message_id is None or draft != str(message_id):
            # Already sent, cancelled, replaced by a newer draft, or drafted
            # before a restart.
            await event.answer()
            await event.edit(t.text("broadcast.stale"))
            return False
        chat_id = event.chat_id
        message = await self._client.get_messages(chat_id, ids=message_id)
        if message is None:
            self._drafts.pop(event.sender_id, None)
            await event.edit(t.text("broadcast.deleted"))
            return False
        # Forwarding the admin's own message would show everyone who sent it.
        forward = forward and _forwarded(message)
        await event.edit(t.text("broadcast.started"))
        # Only now, once it is sure to go: a failure before here leaves the
        # draft for the admin to confirm again.
        self._drafts.pop(event.sender_id, None)

        async def refetch() -> Any:
            return await self._client.get_messages(chat_id, ids=message_id)

        task = asyncio.create_task(self._broadcast(event, message, t, forward, refetch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _broadcast(
        self, event: Any, message: Any, t: Locale, forward: bool, refetch: Refetch
    ) -> None:
        async def report(tally: Tally) -> None:
            await _quietly(event.edit(t.text("broadcast.progress", tally=tally.describe(t))))

        try:
            tally = await self._broadcaster.send(message, report, forward=forward, refetch=refetch)
        except Exception:
            log.exception("broadcast failed")
            await _quietly(event.edit(t.text("broadcast.failed")))
            return
        await _quietly(event.edit(t.text("broadcast.finished", tally=tally.describe(t))))


def _forwarded(message: Any) -> bool:
    return getattr(message, "fwd_from", None) is not None


def _state(on: bool, t: Locale) -> Html:
    return t.word("admin.on" if on else "admin.off")


def _switch_style(on: bool) -> Style | None:
    """A switch that is on is green; one that is off looks like any button."""
    return SUCCESS if on else None


async def _alert(event: Any, t: Locale, problem: Html) -> None:
    await event.answer(plain(t.lines(problem))[:ALERT_LIMIT], alert=True)


async def _quietly(edit: Awaitable[Any]) -> None:
    try:
        await edit
    except Exception:
        log.debug("admin message edit failed", exc_info=True)
