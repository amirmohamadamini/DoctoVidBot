"""Running one job: fetch the file, make it playable if needed, send it back."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from collections.abc import Callable
from html import escape
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError

from . import media, progress
from .config import Config
from .jobs import Job, Mode
from .names import extension, with_extension
from .quota import Calendar
from .safefile import UnsafeFile, claim, read_small
from .store import Store
from .tools import LocalTools, MediaTools, WorkerUnavailable
from .transport import DANGER, Key, Keyboard, Transport

log = logging.getLogger(__name__)

#: The runner's own files in a job's directory. Fixed names, never the user's,
#: so a file the user named `playable.mp4` can't collide with the remux output.
#: The name the user sees travels separately, to the upload.
SOURCE_FILE = "source"
PLAYABLE_FILE = "playable.mp4"
THUMBNAIL_FILE = "thumbnail.jpg"

#: Left free for the database, the session and thumbnails.
DISK_MARGIN_BYTES = 256 * 1024 * 1024

#: Telegram rejects thumbnails over 200 KB; anything much bigger isn't one.
THUMBNAIL_READ_LIMIT = 1024 * 1024


def job_directory(root: Path, job_id: int) -> Path:
    """A fresh directory for one job, under a name nobody can guess.

    The media worker can enter a job's directory but not list the scratch
    directory, so an unguessable name keeps one job's files out of reach of a
    worker compromised while handling another.
    """
    return root / f"job{job_id}-{secrets.token_hex(8)}"


class JobFailed(Exception):
    """A failure with a message fit to show the person who sent the file."""


def cancel_button(job_id: int) -> Keyboard:
    return [[Key("✖️ Cancel", f"stop:{job_id}", DANGER)]]


def space_needed(job: Job) -> int:
    """A remux writes its MP4 next to the download, so a video may need two copies."""
    return job.request.size * (2 if job.mode is Mode.VIDEO else 1)


def free_disk(path: Path) -> int:
    return shutil.disk_usage(path).free


def cache_key(job: Job) -> str | None:
    """Only a video made playable, and only as it came out.

    That is where the cache saves real work. "Send as file" is left out: it is
    most often someone's own document, and re-sending one costs nothing. A
    custom name or thumbnail means a different file, so it's left out too.
    """
    if job.mode is not Mode.VIDEO or job.request.customised:
        return None
    return f"{job.request.document_id}:{job.mode}"


class Runner:
    def __init__(
        self,
        config: Config,
        store: Store,
        transport: Transport,
        *,
        now: Callable[[], float] = time.monotonic,
        free_bytes: Callable[[Path], int] = free_disk,
        calendar: Calendar | None = None,
        tools: MediaTools | None = None,
    ) -> None:
        self._config = config
        self._tools = tools or LocalTools()
        self._calendar = calendar or Calendar(config.timezone)
        self._store = store
        self._telegram = transport
        self._now = now
        self._free_bytes = free_bytes
        #: Bytes promised to jobs already running. Their files are still
        #: growing, so free space alone would let every worker start a large
        #: download at once and all of them run out of room halfway.
        self._reserved = 0

    async def __call__(self, job: Job) -> None:
        workdir = job_directory(self._config.work_dir, job.id)
        workdir.mkdir(parents=True)
        self._tools.share_dir(workdir)
        try:
            await self._run(job, workdir)
        except asyncio.CancelledError:
            await self._status(job, "✖️ Cancelled.")
            raise
        except JobFailed as exc:
            await self._status(job, f"⚠️ {exc}")
        except WorkerUnavailable:
            log.exception("job %s: the media worker failed", job.id)
            await self._status(
                job, "⚠️ The video converter isn't available right now. Please try again later."
            )
        except FloodWaitError as exc:
            await self._status(
                job,
                f"⚠️ Telegram is limiting this bot right now. Try again in "
                f"{progress.clock(exc.seconds)}.",
            )
        except RPCError as exc:
            log.warning("job %s: telegram refused: %s", job.id, exc)
            await self._status(job, "⚠️ Telegram refused that request. Please try again later.")
        except Exception:
            log.exception("job %s failed", job.id)
            await self._status(job, "⚠️ Something went wrong on our side. Please try again later.")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if job.request.thumbnail:
                Path(job.request.thumbnail).unlink(missing_ok=True)

    async def _run(self, job: Job, workdir: Path) -> None:
        key = cache_key(job)
        if key is not None and await self._from_cache(job, key):
            self._count(job)
            return

        needed = space_needed(job)
        if self._free_bytes(workdir) - self._reserved - needed < DISK_MARGIN_BYTES:
            raise JobFailed("The server is short of disk space right now. Try again later.")
        self._reserved += needed
        try:
            message_id = await self._process(job, workdir)
        finally:
            self._reserved -= needed
        self._count(job)

        await self._finish(job)
        if key is not None:
            await self._to_cache(job, key, message_id)

    # ------------------------------------------------------------ stages

    async def _process(self, job: Job, workdir: Path) -> int:
        source = workdir / SOURCE_FILE
        await self._download(job, str(source))
        if job.mode is Mode.VIDEO:
            return await self._send_video(job, source, workdir)
        return await self._upload(
            job,
            source,
            job.request.output_name,
            video=None,
            thumbnail=self._thumbnail_bytes(job.request.thumbnail),
        )

    async def _download(self, job: Job, dest: str) -> None:
        await self._status(
            job, f"⬇️ Downloading <b>{escape(job.request.file_name)}</b>…", cancellable=True
        )
        report = self._transfer_bar(job, "⬇️ Downloading", job.request.file_name)
        found = await self._telegram.download(job.user_id, job.request.message_id, dest, report)
        if not found:
            raise JobFailed("That file is no longer in this chat. Send it again.")

    async def _send_video(self, job: Job, source: Path, workdir: Path) -> int:
        self._tools.share_file(source)
        info = await self._tools.probe(source)
        if info is None or not info.is_video:
            raise JobFailed("That doesn't look like a video, so it can't be made playable.")

        name = job.request.output_name
        match info.plan:
            case media.Plan.UNSUPPORTED:
                raise JobFailed(
                    f"This video uses {escape(info.video_codec or 'an unknown codec')}, which "
                    "Telegram can't play. Making it playable would mean re-encoding it, "
                    "which this bot doesn't do."
                )
            case media.Plan.REMUX:
                target = workdir / PLAYABLE_FILE
                report = self._remux_bar(job, name)
                if not await self._tools.remux(source, target, info, report):
                    raise JobFailed("Repacking that video failed. It may be damaged.")
                source = target
                name = with_extension(name, ".mp4")
            case media.Plan.READY:
                # Already an MP4 with streams Telegram plays: sent as it is.
                if extension(name) not in {".mp4", ".m4v", ".mov"}:
                    name = with_extension(name, ".mp4")

        thumbnail = self._thumbnail_bytes(job.request.thumbnail)
        if thumbnail is None:
            frame = workdir / THUMBNAIL_FILE
            if await self._tools.frame_thumbnail(source, frame, info.duration_s):
                thumbnail = self._thumbnail_bytes(str(frame))

        return await self._upload(job, source, name, video=info, thumbnail=thumbnail)

    def _thumbnail_bytes(self, path: str | None) -> bytes | None:
        """A thumbnail is optional: one that can't be read safely is dropped."""
        if path is None:
            return None
        try:
            return read_small(path, THUMBNAIL_READ_LIMIT)
        except UnsafeFile:
            log.warning("ignoring thumbnail %s", path, exc_info=True)
            return None

    async def _upload(
        self,
        job: Job,
        path: Path,
        name: str,
        *,
        video: media.MediaInfo | None,
        thumbnail: bytes | None,
    ) -> int:
        # Opened here, once, without following links: in a directory the
        # media worker can write to, the path alone proves nothing (see
        # safefile.py). The size checked is the size of what gets uploaded.
        try:
            file = claim(path)
        except UnsafeFile as exc:
            log.error("job %s: refusing to upload %s: %s", job.id, path, exc)
            raise JobFailed("Something went wrong preparing that file.") from exc
        with file:
            size = os.fstat(file.fileno()).st_size
            # Converting its audio can make a video bigger than it was, and
            # past Telegram's limit the upload would only fail later.
            if size > self._config.max_file_bytes:
                raise JobFailed(
                    f"Made playable, that file would be {progress.size(size)}, over the "
                    f"{progress.size(self._config.max_file_bytes)} Telegram allows."
                )
            await self._status(job, f"⬆️ Uploading <b>{escape(name)}</b>…", cancellable=True)
            return await self._telegram.upload(
                job.user_id,
                file,
                size=size,
                name=name,
                video=video,
                thumbnail=thumbnail,
                reply_to=job.request.message_id,
                progress=self._transfer_bar(job, "⬆️ Uploading", name),
            )

    async def _finish(self, job: Job) -> None:
        try:
            await self._telegram.delete(job.user_id, job.status_message_id)
        except Exception:
            log.debug("could not delete status message", exc_info=True)

    def _count(self, job: Job) -> None:
        """Counted once delivered: a failed or cancelled job costs nothing."""
        self._store.record(job.user_id, self._calendar.today(), job.request.size)

    # ------------------------------------------------------------ cache

    def _cache_channel(self) -> int | None:
        if self._config.cache_channel_id is None or not self._store.cache_enabled():
            return None
        return self._config.cache_channel_id

    async def _from_cache(self, job: Job, key: str) -> bool:
        channel = self._cache_channel()
        message_id = self._store.cached(key)
        if channel is None or message_id is None:
            return False
        try:
            sent = await self._telegram.copy(
                channel, message_id, job.user_id, reply_to=job.request.message_id
            )
        except Exception:
            # The cache only ever saves work. If it can't, the file is
            # processed as though it had never been seen.
            log.warning("cache copy failed for %s", key, exc_info=True)
            return False
        if sent is None:
            self._store.forget(key)
            return False
        log.info("job %s served from cache", job.id)
        await self._finish(job)
        return True

    async def _to_cache(self, job: Job, key: str, message_id: int) -> None:
        """Best effort: the user already has their file, whatever happens here."""
        channel = self._cache_channel()
        if channel is None:
            return
        try:
            stored = await self._telegram.copy(job.user_id, message_id, channel)
        except Exception:
            log.warning("could not store job %s in the cache channel", job.id, exc_info=True)
            return
        if stored is not None:
            self._store.remember(key, stored)

    # ------------------------------------------------------------ progress

    async def _status(self, job: Job, text: str, *, cancellable: bool = False) -> None:
        buttons = cancel_button(job.id) if cancellable else None
        try:
            await self._telegram.edit(job.user_id, job.status_message_id, text, buttons)
        except Exception:
            log.debug("status edit failed", exc_info=True)

    def _transfer_bar(self, job: Job, verb: str, name: str) -> progress.ProgressCallback:
        throttle = progress.Throttle(self._config.progress_interval_s, self._now)
        speed = progress.Speed(self._now)

        async def report(done: int, total: int) -> None:
            if not throttle.due():
                return
            speed.update(done)
            text = progress.transfer(verb, name, done, total, speed.value)
            if throttle.admit(text):
                await self._edit_quietly(job, text)

        return report

    def _remux_bar(self, job: Job, name: str) -> progress.ProgressCallback:
        throttle = progress.Throttle(self._config.progress_interval_s, self._now)

        async def report(done_s: int, total_s: int) -> None:
            text = progress.remux(name, done_s, total_s)
            if throttle.admit(text):
                await self._edit_quietly(job, text)

        return report

    async def _edit_quietly(self, job: Job, text: str) -> None:
        # A failed progress edit must never fail the transfer it describes.
        try:
            await self._telegram.edit(
                job.user_id, job.status_message_id, text, cancel_button(job.id)
            )
        except Exception:
            log.debug("progress edit failed", exc_info=True)
