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

from . import menus, progress, quota
from .admin import Admin
from .backup import Backups
from .broadcast import Broadcaster
from .config import Config
from .jobs import Job, JobQueue, Mode, QueueFull, Request, UserLimit
from .membership import MEMBERS_FIX, POSTING_FIX, Membership, channel_problem
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
COMMANDS = {"start": "Show the menu"}


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
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._now = now
        self._calendar = calendar or quota.Calendar(config.timezone)
        self._telegram = TelethonTransport(client)
        self._tools = tools or LocalTools()
        self._queue = JobQueue(
            Runner(
                config, store, self._telegram, now=now, calendar=self._calendar, tools=self._tools
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
        )
        self._admin = Admin(
            client,
            config,
            store,
            broadcaster=Broadcaster(client, store),
            backups=self._backups,
            membership=self._membership,
            activity=self.activity,
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
            self._on_button,
            events.CallbackQuery(pattern=rb"^(video|file|name|thumb|nothumb|drop|stop|joined):"),
        )
        self._queue.start()
        self._background = [
            asyncio.create_task(self._sweep_forever(), name="sweeper"),
            asyncio.create_task(self._backups.run_forever(), name="backups"),
        ]

    async def publish_commands(self) -> None:
        try:
            await self._client(
                SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                    commands=[BotCommand(name, about) for name, about in COMMANDS.items()],
                )
            )
        except Exception:
            log.warning("could not publish the command list", exc_info=True)

    async def check_channels(self) -> None:
        """Say at startup, in the log, if a configured channel can't be reached.

        The admin panel checks again when a switch is turned on; this is for the
        admin reading the log after a deploy, before anyone taps anything.
        """
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
                log.warning("%s channel %s: %s", label, channel, channel_problem(exc, fix=fix))

    async def stop(self) -> None:
        # Everything that might still touch the database is stopped and waited
        # for, because the caller closes it next.
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self._admin.stop()
        await self._queue.stop()

    def activity(self) -> str:
        return (
            f"Running: {self._queue.running} of {self._config.workers}\n"
            f"Waiting: {self._queue.waiting} (at most {self._config.max_queue})\n"
            f"Open menus: {len(self._requests)}"
        )

    # ------------------------------------------------------------ commands

    async def _on_anything(self, event: Any) -> None:
        self._store.seen(event.sender_id)

    async def _on_start(self, event: Any) -> None:
        await event.respond(
            menus.welcome(self._config.max_file_mb, self._admin.cache_on()),
            buttons=self._keyboard(event.sender_id),
            link_preview=False,
        )
        raise events.StopPropagation

    def _keyboard(self, user_id: int) -> list[list[Any]]:
        return to_keyboard(menus.main_keyboard(self._admin.is_admin(user_id)))

    async def _on_keyboard(self, event: Any, label: str) -> None:
        user_id = event.sender_id
        if label == menus.HELP_BUTTON:
            text = menus.HELP
            if self._admin.is_admin(user_id):
                text += f"\n\nYou're an admin: tap {menus.ADMIN_BUTTON} for the admin panel."
            await event.respond(text, link_preview=False)
        elif label == menus.USAGE_BUTTON:
            today, this_month = self._store.usage(user_id, self._calendar.today())
            await event.respond(quota.summary(self._config.limits_for(user_id), today, this_month))
        elif label == menus.ADMIN_BUTTON and self._admin.is_admin(user_id):
            await self._admin.open_panel(event)
        else:
            await self._nudge(event)

    async def _nudge(self, event: Any) -> None:
        """Anything the bot doesn't understand. The keyboard goes with it, so
        someone who never sent /start still ends up with the buttons."""
        await event.respond(
            f"👋 Send me a file, or tap {menus.HELP_BUTTON}.",
            buttons=self._keyboard(event.sender_id),
        )

    def _over_limit(self, user_id: int, size: int) -> str | None:
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
        )

    # ------------------------------------------------------------ messages

    async def _on_message(self, event: Any) -> None:
        message = event.message
        if message.raw_text in menus.KEYBOARD_LABELS:
            await self._on_keyboard(event, message.raw_text)
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
            await event.reply(
                "📷 That's a photo, and Telegram strips photos of their name. "
                "Send it as a file to rename it."
            )
            return
        if message.document and not (message.sticker or message.voice or message.video_note):
            await self._take_file(event)
            return
        await self._nudge(event)

    async def _is_allowed(self, user_id: int) -> bool:
        """The join-the-channel rule, when an admin has turned it on."""
        if self._admin.is_admin(user_id) or not self._admin.is_joining_required():
            return True
        assert self._membership is not None
        return await self._membership.is_member(user_id)

    async def _may_use(self, event: Any) -> bool:
        if await self._is_allowed(event.sender_id):
            return True
        assert self._config.join_link is not None
        text, keyboard = menus.join_required(self._config.join_link)
        await event.reply(text, buttons=to_buttons(keyboard))
        return False

    async def _take_file(self, event: Any) -> None:
        message = event.message
        user_id = event.sender_id
        size = message.file.size or 0
        if size > self._config.max_file_bytes:
            await event.reply(
                f"📦 That file is {progress.size(size)}. The most I can take is "
                f"{progress.size(self._config.max_file_bytes)}."
            )
            return
        if reason := self._over_limit(user_id, size):
            await event.reply(f"🚫 {reason}")
            return
        if sum(1 for r in self._requests.values() if r.user_id == user_id) >= MAX_OPEN_MENUS:
            await event.reply("✋ Finish or cancel one of your open files first.")
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
        text, keyboard = menus.request_menu(request)
        menu = await event.reply(text, buttons=to_buttons(keyboard))
        request.menu_message_id = menu.id
        self._requests[request.id] = request

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
        size = event.message.file.size or 0
        if size > THUMBNAIL_MAX_BYTES:
            await event.reply(
                f"⚠️ That picture is {progress.size(size)}. A thumbnail can be at most "
                f"{progress.size(THUMBNAIL_MAX_BYTES)}. Tap 🖼 Thumbnail to try a smaller one."
            )
            return
        if not self._may_try_thumbnail(event.sender_id):
            await event.reply(
                "⏳ That's a lot of thumbnails. Try again in a little while, or send the "
                "file back without one."
            )
            return
        fitted = await self._fit_thumbnail(event, request)
        if fitted is None:
            await event.reply("⚠️ I couldn't use that picture. Tap 🖼 Thumbnail to try another.")
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

        request = self._requests.get(target)
        if request is None or request.user_id != event.sender_id:
            await event.answer("This menu has expired. Send the file again.", alert=True)
            return

        match action:
            case "video" | "file":
                await self._submit(event, request, Mode(action))
            case "name":
                await self._ask(
                    event,
                    request,
                    "name",
                    menus.rename_prompt(request.output_name),
                    hint=request.output_name,
                )
            case "thumb":
                await self._ask(
                    event, request, "thumb", menus.THUMBNAIL_PROMPT, hint="Send a photo"
                )
            case "nothumb":
                _discard(request.thumbnail)
                request.thumbnail = None
                await event.answer("Thumbnail removed.")
                await self._show_menu(request)
            case "drop":
                self._close(request)
                await event.answer()
                await self._edit(request.user_id, request.menu_message_id, "✖️ Cancelled.")
            case _:
                await event.answer()

    async def _submit(self, event: Any, request: Request, mode: Mode) -> None:
        # Checked again when it matters: the menu may have been opened before
        # the join rule was turned on, or before they left the channel.
        if not await self._is_allowed(request.user_id):
            text, _ = menus.join_required(self._config.join_link or "")
            await event.answer(text, alert=True)
            return
        # Also again: other files may have been sent since this menu opened.
        if reason := self._over_limit(request.user_id, request.size):
            await event.answer(reason, alert=True)
            return
        job = Job(id=self._queue.next_id(), request=request, mode=mode)
        try:
            position = await self._queue.submit(job)
        except UserLimit:
            await event.answer(
                f"You already have {self._config.jobs_per_user} files on the way. "
                "Wait for one to finish.",
                alert=True,
            )
            return
        except QueueFull:
            await event.answer("I'm very busy right now. Try again in a few minutes.", alert=True)
            return
        # The request now belongs to the job, which cleans up its thumbnail.
        self._requests.pop(request.id, None)
        self._drop_prompt_for(request)
        # Replace the menu before anything else awaits, so the runner's first
        # status lands after this one rather than being overwritten by it.
        await self._show_position(job, position)
        await event.answer()

    async def _check_joined(self, event: Any) -> None:
        if self._membership is None or await self._membership.is_member(event.sender_id):
            await event.answer("Thanks for joining!")
            await event.edit("✅ Thanks for joining. Now send me your file.")
        else:
            await event.answer(
                "I can't see you in the channel yet. Join it, then tap again.", alert=True
            )

    async def _stop_job(self, event: Any, job_id: int) -> None:
        job = self._queue.find(job_id)
        if job is None or job.user_id != event.sender_id:
            await event.answer("That's already finished.")
            return
        self._queue.cancel(job_id)
        await event.answer("Cancelled.")
        # A running job says so itself, but one cancelled before it got going
        # never runs the code that would.
        _discard(job.request.thumbnail)
        await self._edit(job.user_id, job.status_message_id, "✖️ Cancelled.")

    async def _ask(self, event: Any, request: Request, kind: str, text: str, *, hint: str) -> None:
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
        await self._edit(
            job.user_id,
            job.status_message_id,
            menus.queued(job.request.output_name, position),
            cancel_button(job.id),
        )

    # ------------------------------------------------------------ menus

    async def _show_menu(self, request: Request) -> None:
        text, keyboard = menus.request_menu(request)
        await self._edit(request.user_id, request.menu_message_id, text, keyboard)

    async def _resend_menu(self, request: Request) -> None:
        """A fresh menu at the bottom of the chat, where the user is looking."""
        await self._delete(request.user_id, request.menu_message_id)
        text, keyboard = menus.request_menu(request)
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
        """Close menus nobody answered, and free what they held."""
        now = self._now()
        for request in [r for r in self._requests.values() if r.expires_at <= now]:
            self._close(request)
            await self._edit(
                request.user_id,
                request.menu_message_id,
                "⌛ This menu expired. Send the file again if you still need it.",
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
