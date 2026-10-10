"""Telegram handlers: files in, menus out, button presses into jobs."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.custom import Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from . import i18n, menus, quota
from .admin import Admin
from .backup import Backups
from .broadcast import Broadcaster
from .config import Config
from .i18n import Html, Locale, Locales, plain
from .jobs import Job, JobQueue, Mode, QueueFull, Request, UserLimit
from .membership import MEMBERS_FIX, POSTING_FIX, CheckFailed, Membership, channel_problem
from .names import apply_rename, looks_like_video, safe_name
from .runner import THUMBNAIL_READ_LIMIT, Runner, cancel_button
from .safefile import read_small
from .store import Store
from .tools import LocalTools, MediaTools
from .transport import Keyboard, TelethonTransport, to_buttons, to_keyboard

log = logging.getLogger(__name__)

#: Open menus one person may have at once. Each may hold a thumbnail on disk.
MAX_OPEN_MENUS = 5

SWEEP_INTERVAL_S = 30.0

#: A custom thumbnail is fetched straight away rather than through the queue,
#: so it has its own limits: a small size, a time limit, and only a couple at
#: once across everyone. Telegram reports the size itself, so a sender can't
#: understate it.
THUMBNAIL_MAX_BYTES = 10 * 1024 * 1024
THUMBNAIL_TIMEOUT_S = 30.0
THUMBNAIL_SLOTS = 2
#: Thumbnail attempts per person per hour, successful or not. Without it, two
#: people could keep both slots busy for everyone else indefinitely.
THUMBNAIL_ATTEMPTS_PER_HOUR = 10

#: The only command. Everything else is a button; /start is what Telegram
#: sends when someone first opens the bot, and it brings the keyboard back.
COMMANDS = ("start",)


@dataclass(slots=True)
class Prompt:
    """A question the bot asked and is waiting for the answer to."""

    request_id: int
    kind: str  # "name" or "thumb"
    message_id: int


class Bot:
    def __init__(
        self,
        client: TelegramClient,
        config: Config,
        store: Store,
        *,
        now: Callable[[], float] = time.monotonic,
        calendar: quota.Calendar | None = None,
        tools: MediaTools | None = None,
        locales: Locales | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._locales = locales or i18n.load(config.default_language)
        self._keyboard_actions = menus.keyboard_actions(self._locales)
        self._now = now
        self._calendar = calendar or quota.Calendar(config.timezone)
        self._telegram = TelethonTransport(client)
        self._tools = tools or LocalTools()
        self._queue = JobQueue(
            Runner(
                config,
                store,
                self._telegram,
                now=now,
                calendar=self._calendar,
                tools=self._tools,
                locale_for=self.locale_for,
            ),
            workers=config.workers,
            max_waiting=config.max_queue,
            per_user=config.jobs_per_user,
            on_position=self._show_position,
        )
        self._requests: dict[int, Request] = {}
        self._prompts: dict[int, Prompt] = {}
        self._request_ids = itertools.count(1)
        self._background: list[asyncio.Task[None]] = []
        self._thumbs = config.thumbnail_dir
        self._thumbnail_slots = asyncio.Semaphore(THUMBNAIL_SLOTS)
        self._thumbnail_attempts: dict[int, list[float]] = {}
        self._membership = (
            None
            if config.join_channel is None
            else Membership(client, config.join_channel, now=now)
        )
        self._backups = Backups(
            client,
            store,
            admins=config.admin_ids,
            interval_hours=config.backup_interval_hours,
            directory=config.backup_dir,
            calendar_now=self._calendar.now,
            locale_for=self.locale_for,
        )
        self._admin = Admin(
            client,
            config,
            store,
            broadcaster=Broadcaster(client, store),
            backups=self._backups,
            membership=self._membership,
            activity=self.activity,
            locale_for=self.locale_for,
            is_keyboard_label=self._keyboard_actions.__contains__,
        )

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thumbs.mkdir(parents=True, exist_ok=True)
        # Registration order matters. Everyone is remembered first, for
        # broadcasts. /start and the admin's handlers stop propagation, so the
        # catch-all message handler at the end only sees what they didn't claim.
        self._client.add_event_handler(
            self._on_anything, events.NewMessage(incoming=True, func=_private)
        )
        self._client.add_event_handler(
            self._on_start, events.NewMessage(pattern=r"^/start\b", func=_private)
        )
        self._admin.register()
        self._client.add_event_handler(
            self._on_message, events.NewMessage(incoming=True, func=_private)
        )
        self._client.add_event_handler(
            self._on_edit, events.MessageEdited(incoming=True, func=_private)
        )
        self._client.add_event_handler(
            self._on_button,
            events.CallbackQuery(
                pattern=rb"^(video|file|name|thumb|nothumb|drop|stop|joined|lang):"
            ),
        )
        self._queue.start()
        self._background = [
            asyncio.create_task(self._sweep_forever(), name="sweeper"),
            asyncio.create_task(self._backups.run_forever(), name="backups"),
        ]

    async def publish_commands(self) -> None:
        """The command menu, in the language of each person's Telegram app, and
        in the default language for everyone else."""
        lists = [("", self._locales.default)] + [(t.code, t) for t in self._locales]
        for lang_code, t in lists:
            try:
                await self._client(
                    SetBotCommandsRequest(
                        scope=BotCommandScopeDefault(),
                        lang_code=lang_code,
                        commands=[
                            BotCommand(name, t.label(f"command.{name}")) for name in COMMANDS
                        ],
                    )
                )
            except Exception:
                log.warning("could not publish the %r command list", lang_code, exc_info=True)

    async def check_channels(self) -> None:
        """Say at startup, in the log, if a configured channel can't be reached.

        The admin panel checks again when a switch is turned on; this is for the
        admin reading the log after a deploy, before anyone taps anything.
        """
        english = self._locales.get(i18n.REFERENCE)
        channels = [
            ("cache", self._config.cache_channel_id, POSTING_FIX),
            ("required", self._config.join_channel, MEMBERS_FIX),
        ]
        for label, channel, fix in channels:
            if channel is None:
                continue
            try:
                await self._client.get_input_entity(channel)
            except Exception as exc:
                problem = plain(channel_problem(exc, fix=fix, t=english))
                log.warning("%s channel %s: %s", label, channel, problem)

    async def stop(self) -> None:
        # Everything that might still touch the database is stopped and waited
        # for, because the caller closes it next.
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self._admin.stop()
        await self._queue.stop()

    def activity(self, t: Locale) -> Html:
        return Html(
            "\n".join(
                [
                    t.phrase(
                        "admin.running", count=self._queue.running, workers=self._config.workers
                    ),
                    t.phrase(
                        "admin.waiting", count=self._queue.waiting, limit=self._config.max_queue
                    ),
                    t.phrase("admin.menus", count=len(self._requests)),
                ]
            )
        )

    def locale_for(self, user_id: int) -> Locale:
        return self._locales.get(self._store.language(user_id))

    # ------------------------------------------------------------ commands

    async def _on_anything(self, event: Any) -> None:
        self._store.seen(event.sender_id)

    async def _on_start(self, event: Any) -> None:
        # The first time, the language comes first: everything after it is
        # in the language chosen.
        if self._store.language(event.sender_id) is None:
            await self._ask_language(event)
        else:
            await self._welcome(event)
        raise events.StopPropagation

    async def _welcome(self, event: Any) -> None:
        t = self.locale_for(event.sender_id)
        await event.respond(
            menus.welcome(t, self._config.max_file_mb, self._admin.cache_on()),
            buttons=self._keyboard(event.sender_id),
            link_preview=False,
        )

    async def _ask_language(self, event: Any) -> None:
        chosen = self._store.language(event.sender_id)
        current = self._locales.get(chosen) if chosen else None
        text, keyboard = menus.language_picker(self._locales, current)
        await event.respond(text, buttons=to_buttons(keyboard))

    async def _set_language(self, event: Any, code: str) -> None:
        if code not in self._locales:
            await event.answer()
            return
        self._store.set_language(event.sender_id, code)
        t = self.locale_for(event.sender_id)
        await event.answer()
        await event.edit(t.text("language.chosen"))
        # The keyboard is redrawn in the new language along with it.
        await self._welcome(event)

    def _keyboard(self, user_id: int) -> list[list[Any]]:
        t = self.locale_for(user_id)
        return to_keyboard(
            menus.main_keyboard(t, self._admin.is_admin(user_id)),
            t.label("keyboard.placeholder"),
        )

    async def _on_keyboard(self, event: Any, action: str) -> None:
        user_id = event.sender_id
        t = self.locale_for(user_id)
        if action == "help":
            text = menus.help_text(t, self._admin.is_admin(user_id))
            # Sent with the keyboard: a tap on an old keyboard, drawn in another
            # language, gets the current one back.
            await event.respond(text, buttons=self._keyboard(user_id), link_preview=False)
        elif action == "usage":
            today, this_month = self._store.usage(user_id, self._calendar.today())
            limits = self._config.limits_for(user_id)
            await event.respond(quota.summary(limits, today, this_month, t))
        elif action == "language":
            await self._ask_language(event)
        elif action == "admin" and self._admin.is_admin(user_id):
            await self._admin.open_panel(event)
        else:
            await self._nudge(event)

    async def _nudge(self, event: Any) -> None:
        """Anything the bot doesn't understand. The keyboard goes with it, so
        someone who never sent /start still ends up with the buttons."""
        t = self.locale_for(event.sender_id)
        await event.respond(
            t.text("start.nudge", help=t.word("keyboard.help")),
            buttons=self._keyboard(event.sender_id),
        )

    def _over_limit(self, user_id: int, size: int) -> Html | None:
        """Files still queued or running count too, or a queue could hold far
        more than anyone is allowed."""
        today, this_month = self._store.usage(user_id, self._calendar.today())
        pending = self._queue.jobs_for(user_id)
        files, volume = len(pending), sum(job.request.size for job in pending)
        return quota.refusal(
            self._config.limits_for(user_id),
            today=today.plus(files, volume),
            this_month=this_month.plus(files, volume),
            size=size,
            calendar=self._calendar,
            t=self.locale_for(user_id),
        )

    # ------------------------------------------------------------ messages

    async def _on_message(self, event: Any) -> None:
        message = event.message
        if action := self._keyboard_actions.get(message.raw_text):
            await self._on_keyboard(event, action)
            return
        if message.raw_text.startswith("/"):
            await self._nudge(event)
            return
        prompt = self._prompts.get(event.sender_id)
        if prompt is not None and prompt.kind == "thumb" and _is_picture(message):
            await self._take_thumbnail(event, prompt)
            return
        if prompt is not None and prompt.kind == "name" and message.raw_text and not message.file:
            await self._take_name(event, prompt)
            return
        if not await self._may_use(event):
            return
        if message.photo:
            await event.reply(self.locale_for(event.sender_id).text("file.photo"))
            return
        if message.document and not (message.sticker or message.voice or message.video_note):
            await self._take_file(event)
            return
        await self._nudge(event)

    async def _is_allowed(self, user_id: int) -> bool:
        """The join-the-channel rule, when an admin has turned it on. Raises
        CheckFailed when Telegram couldn't say."""
        if self._admin.is_admin(user_id) or not self._admin.is_joining_required():
            return True
        assert self._membership is not None
        return await self._membership.is_member(user_id)

    async def _may_use(self, event: Any) -> bool:
        try:
            if await self._is_allowed(event.sender_id):
                return True
        except CheckFailed:
            await event.reply(self.locale_for(event.sender_id).text("join.check_failed"))
            return False
        assert self._config.join_link is not None
        text, keyboard = menus.join_required(
            self._config.join_link, self.locale_for(event.sender_id)
        )
        await event.reply(text, buttons=to_buttons(keyboard))
        return False

    async def _take_file(self, event: Any) -> None:
        message = event.message
        user_id = event.sender_id
        t = self.locale_for(user_id)
        size = message.file.size or 0
        if size > self._config.max_file_bytes:
            limit = t.size(self._config.max_file_bytes)
            await event.reply(t.text("file.too_big", size=t.size(size), limit=limit))
            return
        if reason := self._over_limit(user_id, size):
            await event.reply(t.text("file.over_limit", reason=reason))
            return
        if sum(1 for r in self._requests.values() if r.user_id == user_id) >= MAX_OPEN_MENUS:
            await event.reply(t.text("file.too_many_open"))
            return

        name = safe_name(message.file.name or _default_name(message))
        request = Request(
            id=next(self._request_ids),
            user_id=user_id,
            message_id=message.id,
            document_id=message.document.id,
            file_name=name,
            size=size,
            mime_type=message.file.mime_type,
            is_video=looks_like_video(name, message.file.mime_type) and not message.gif,
            expires_at=self._now() + self._config.menu_timeout_s,
        )
        text, keyboard = menus.request_menu(request, t)
        menu = await event.reply(text, buttons=to_buttons(keyboard))
        request.menu_message_id = menu.id
        self._requests[request.id] = request

    async def _on_edit(self, event: Any) -> None:
        """A file replaced in its message closes the menu opened for it. A new
        caption on the same file changes nothing."""
        document = event.message.document
        for request in list(self._requests.values()):
            if request.user_id != event.sender_id or request.message_id != event.message.id:
                continue
            if document is not None and document.id == request.document_id:
                continue
            self._close(request)
            t = self.locale_for(request.user_id)
            await self._edit(request.user_id, request.menu_message_id, t.text("menu.replaced"))

    async def _take_name(self, event: Any, prompt: Prompt) -> None:
        del self._prompts[event.sender_id]
        request = self._requests.get(prompt.request_id)
        await self._delete(event.sender_id, prompt.message_id)
        if request is None:
            return
        renamed = apply_rename(request.file_name, event.message.raw_text)
        request.new_name = None if renamed == request.file_name else renamed
        await self._resend_menu(request)

    async def _take_thumbnail(self, event: Any, prompt: Prompt) -> None:
        del self._prompts[event.sender_id]
        request = self._requests.get(prompt.request_id)
        await self._delete(event.sender_id, prompt.message_id)
        if request is None:
            return
        t = self.locale_for(event.sender_id)
        button = t.word("menu.add_thumbnail")
        size = event.message.file.size or 0
        if size > THUMBNAIL_MAX_BYTES:
            limit = t.size(THUMBNAIL_MAX_BYTES)
            await event.reply(
                t.text("thumbnail.too_big", size=t.size(size), limit=limit, button=button)
            )
            return
        if not self._may_try_thumbnail(event.sender_id):
            await event.reply(t.text("thumbnail.too_many"))
            return
        fitted = await self._fit_thumbnail(event, request)
        if fitted is None:
            await event.reply(t.text("thumbnail.unusable", button=button))
            return
        if request.id not in self._requests:
            # Sent on its way, or cancelled, while the picture was downloading.
            fitted.unlink(missing_ok=True)
            return
        request.thumbnail = str(fitted)
        await self._resend_menu(request)

    def _may_try_thumbnail(self, user_id: int) -> bool:
        """Counts the attempt, if it's allowed."""
        hour_ago = self._now() - 3600
        recent = [t for t in self._thumbnail_attempts.get(user_id, []) if t > hour_ago]
        if len(recent) >= THUMBNAIL_ATTEMPTS_PER_HOUR:
            self._thumbnail_attempts[user_id] = recent
            return False
        self._thumbnail_attempts[user_id] = [*recent, self._now()]
        return True

    async def _fit_thumbnail(self, event: Any, request: Request) -> Path | None:
        """Download and shrink the picture. On any failure, nothing is left behind.

        The work happens in a scratch directory of its own, which the media
        worker can reach. Only the finished thumbnail, read back safely, is
        kept, in a directory only the bot can read.
        """
        attempt = self._config.work_dir / f"thumb-{secrets.token_hex(8)}"
        final = self._thumbs / f"{request.id}.jpg"
        done = False
        try:
            async with asyncio.timeout(THUMBNAIL_TIMEOUT_S), self._thumbnail_slots:
                attempt.mkdir(parents=True)
                self._tools.share_dir(attempt)
                # With an extension, so Telethon doesn't add one of its own.
                picture = attempt / "picture.upload"
                fitted = attempt / "thumbnail.jpg"
                await event.message.download_media(file=str(picture))
                self._tools.share_file(picture)
                if await self._tools.fit_thumbnail(picture, fitted):
                    final.write_bytes(read_small(fitted, THUMBNAIL_READ_LIMIT))
                    done = True
        except TimeoutError:
            log.info("thumbnail for request %s timed out", request.id)
        except Exception:
            log.warning("thumbnail for request %s failed", request.id, exc_info=True)
        finally:
            shutil.rmtree(attempt, ignore_errors=True)
            if not done:
                final.unlink(missing_ok=True)
        return final if done else None

    # ------------------------------------------------------------ buttons

    async def _on_button(self, event: Any) -> None:
        action, _, raw_id = event.data.decode(errors="replace").partition(":")
        if action == "lang":
            await self._set_language(event, raw_id)
            return
        try:
            target = int(raw_id)
        except ValueError:
            await event.answer()
            return
        if action == "stop":
            await self._stop_job(event, target)
            return
        if action == "joined":
            await self._check_joined(event)
            return

        t = self.locale_for(event.sender_id)
        request = self._requests.get(target)
        if request is None or request.user_id != event.sender_id:
            await event.answer(t.alert("menu.expired_alert"), alert=True)
            return

        match action:
            case "video" | "file":
                await self._submit(event, request, Mode(action))
            case "name":
                prompt = t.text("menu.rename_prompt", name=request.output_name)
                await self._ask(event, request, "name", prompt, hint=request.output_name)
            case "thumb":
                prompt = t.text("menu.thumbnail_prompt")
                hint = t.label("menu.thumbnail_hint")
                await self._ask(event, request, "thumb", prompt, hint=hint)
            case "nothumb":
                _discard(request.thumbnail)
                request.thumbnail = None
                await event.answer(t.alert("menu.thumbnail_removed"))
                await self._show_menu(request)
            case "drop":
                self._close(request)
                await event.answer()
                await self._edit(request.user_id, request.menu_message_id, t.text("menu.cancelled"))
            case _:
                await event.answer()

    async def _submit(self, event: Any, request: Request, mode: Mode) -> None:
        t = self.locale_for(request.user_id)
        # Checked again when it matters: the menu may have been opened before
        # the join rule was turned on, or before they left the channel.
        try:
            allowed = await self._is_allowed(request.user_id)
        except CheckFailed:
            await event.answer(t.alert("join.check_failed"), alert=True)
            return
        if not allowed:
            await event.answer(t.alert("join.prompt"), alert=True)
            return
        # Also again: other files may have been sent since this menu opened.
        if reason := self._over_limit(request.user_id, request.size):
            await event.answer(plain(t.lines(reason)), alert=True)
            return
        job = Job(id=self._queue.next_id(), request=request, mode=mode)
        try:
            position = await self._queue.submit(job)
        except UserLimit:
            limit = self._config.jobs_per_user
            await event.answer(t.alert("queue.per_user", count=limit), alert=True)
            return
        except QueueFull:
            await event.answer(t.alert("queue.full"), alert=True)
            return
        # The request now belongs to the job, which cleans up its thumbnail.
        self._requests.pop(request.id, None)
        self._drop_prompt_for(request)
        # Replace the menu before anything else awaits, so the runner's first
        # status lands after this one rather than being overwritten by it.
        await self._show_position(job, position)
        await event.answer()

    async def _check_joined(self, event: Any) -> None:
        t = self.locale_for(event.sender_id)
        try:
            member = self._membership is None or await self._membership.is_member(event.sender_id)
        except CheckFailed:
            await event.answer(t.alert("join.check_failed"), alert=True)
            return
        if member:
            await event.answer(t.alert("join.thanks_alert"))
            await event.edit(t.text("join.thanks"))
        else:
            await event.answer(t.alert("join.not_yet"), alert=True)

    async def _stop_job(self, event: Any, job_id: int) -> None:
        t = self.locale_for(event.sender_id)
        job = self._queue.find(job_id)
        if job is None or job.user_id != event.sender_id:
            await event.answer(t.alert("queue.finished"))
            return
        self._queue.cancel(job_id)
        await event.answer(t.alert("queue.stopped"))
        # A running job says so itself, but one cancelled before it got going
        # never runs the code that would.
        _discard(job.request.thumbnail)
        await self._edit(job.user_id, job.status_message_id, t.text("menu.cancelled"))

    async def _ask(self, event: Any, request: Request, kind: str, text: Html, *, hint: str) -> None:
        old = self._prompts.pop(request.user_id, None)
        if old is not None:
            await self._delete(request.user_id, old.message_id)
        await event.answer()
        sent = await self._client.send_message(
            request.user_id,
            text,
            # The hint sits in the empty message box. For a rename it is the
            # current name, which is what most people are about to edit.
            buttons=Button.force_reply(selective=True, placeholder=hint[:64]),
            reply_to=request.message_id,
        )
        self._prompts[request.user_id] = Prompt(request.id, kind, sent.id)

    async def _show_position(self, job: Job, position: int) -> None:
        t = self.locale_for(job.user_id)
        await self._edit(
            job.user_id,
            job.status_message_id,
            menus.queued(job.request.output_name, position, t),
            cancel_button(job.id, t),
        )

    # ------------------------------------------------------------ menus

    async def _show_menu(self, request: Request) -> None:
        text, keyboard = menus.request_menu(request, self.locale_for(request.user_id))
        await self._edit(request.user_id, request.menu_message_id, text, keyboard)

    async def _resend_menu(self, request: Request) -> None:
        """A fresh menu at the bottom of the chat, where the user is looking."""
        await self._delete(request.user_id, request.menu_message_id)
        text, keyboard = menus.request_menu(request, self.locale_for(request.user_id))
        menu = await self._client.send_message(
            request.user_id, text, buttons=to_buttons(keyboard), reply_to=request.message_id
        )
        request.menu_message_id = menu.id

    def _close(self, request: Request) -> None:
        self._requests.pop(request.id, None)
        self._drop_prompt_for(request)
        _discard(request.thumbnail)

    def _drop_prompt_for(self, request: Request) -> None:
        prompt = self._prompts.get(request.user_id)
        if prompt is not None and prompt.request_id == request.id:
            del self._prompts[request.user_id]

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            await self.sweep()

    async def sweep(self) -> None:
        """Close menus nobody answered, free what they held, and forget
        what is too old to matter, so memory doesn't grow with every user."""
        now = self._now()
        hour_ago = now - 3600
        for user_id, attempts in list(self._thumbnail_attempts.items()):
            if all(at <= hour_ago for at in attempts):
                del self._thumbnail_attempts[user_id]
        if self._membership is not None:
            self._membership.forget_expired()
        for request in [r for r in self._requests.values() if r.expires_at <= now]:
            self._close(request)
            await self._edit(
                request.user_id,
                request.menu_message_id,
                self.locale_for(request.user_id).text("menu.expired"),
            )

    # ------------------------------------------------------------ helpers

    async def _edit(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None:
        try:
            await self._telegram.edit(chat_id, message_id, text, buttons)
        except Exception:
            log.debug("edit failed", exc_info=True)

    async def _delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self._telegram.delete(chat_id, message_id)
        except Exception:
            log.debug("delete failed", exc_info=True)


def _private(event: Any) -> bool:
    return bool(event.is_private)


def _is_picture(message: Any) -> bool:
    if message.photo:
        return True
    mime = message.file.mime_type if message.file else None
    return bool(mime and mime.startswith("image/"))


def _default_name(message: Any) -> str:
    """Telegram sends videos recorded in the app with no file name."""
    return f"video_{message.id}{message.file.ext or '.mp4'}" if message.video else "file"


def _discard(path: str | None) -> None:
    if path:
        with contextlib.suppress(OSError):
            Path(path).unlink()
