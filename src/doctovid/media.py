"""ffprobe and ffmpeg.

Telegram decides whether a video plays inline from its container, not from the
attributes a sender sets: an .mkv is shown as a file to download however it is
uploaded. So the question for every video is which of three things it needs:

* nothing, because it is already an MP4 with streams Telegram plays;
* a remux, which copies the streams into MP4 in seconds without re-encoding;
* a real re-encode, which this bot does not do.

The first case never touches ffmpeg.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import functools
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any

from .progress import ProgressCallback

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 30.0
THUMBNAIL_TIMEOUT_S = 60.0
REMUX_TIMEOUT_S = 1800.0

#: Every ffmpeg and ffprobe run is capped, because each one parses bytes a
#: stranger chose. A crafted file can at worst use this much memory, write this
#: much to disk, and print this much. See `_spawn`. 1 GiB decodes a 4K frame
#: comfortably; a remux copies streams and needs far less.
MEMORY_LIMIT_BYTES = 1024**3
THUMBNAIL_FILE_LIMIT_BYTES = 16 * 1024**2
PROBE_OUTPUT_LIMIT_BYTES = 8 * 1024**2
STDERR_LIMIT_BYTES = 256 * 1024

#: ffmpeg otherwise starts a thread per CPU core, each reserving memory of its
#: own, which on a large server breaks the memory cap above before any work is
#: done. Two is plenty for copying streams or decoding a single frame.
FFMPEG_THREADS = 2

#: The bitrate audio is converted to when MP4 can't carry it as it is. Set
#: rather than left to ffmpeg, because the most a remux can write depends on it.
AAC_BITRATE = 192_000

#: Telegram ignores thumbnails larger than 320 px on either side.
THUMBNAIL_SIDE = 320

#: The largest picture accepted as a thumbnail, in pixels. A small, highly
#: compressed image can describe an enormous one, and decoding it is what
#: costs memory. Telegram's own photos stay well under this.
THUMBNAIL_MAX_PIXELS = 40_000_000

#: Video codecs an MP4 can carry and Telegram's players decode.
PLAYABLE_VIDEO_CODECS = frozenset({"h264", "hevc", "av1", "mpeg4"})

#: Audio codecs that play from an MP4. Anything else is converted to AAC, which
#: is cheap; the alternative is a video that plays without sound.
PLAYABLE_AUDIO_CODECS = frozenset({"aac", "mp3", "alac"})

#: ffprobe reports every ISO media file as "mov,mp4,m4a,3gp,3g2,mj2".
_MP4_FAMILY = frozenset({"mov", "mp4"})


class Plan(enum.Enum):
    READY = "ready"
    REMUX = "remux"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class MediaInfo:
    formats: frozenset[str] = frozenset()
    video_codec: str | None = None
    audio_codecs: tuple[str, ...] = ()
    duration_s: float = 0.0
    width: int = 0
    height: int = 0

    @property
    def is_video(self) -> bool:
        return self.video_codec is not None

    @property
    def plan(self) -> Plan:
        if self.video_codec not in PLAYABLE_VIDEO_CODECS:
            return Plan.UNSUPPORTED
        if self.formats & _MP4_FAMILY and self.audio_fits:
            return Plan.READY
        return Plan.REMUX

    @property
    def audio_fits(self) -> bool:
        return all(codec in PLAYABLE_AUDIO_CODECS for codec in self.audio_codecs)


async def probe(path: str) -> MediaInfo | None:
    """What is in the file, or None if ffprobe could not read it."""
    raw = await run_ffprobe(path)
    return None if raw is None else parse(raw)


async def run_ffprobe(path: str) -> dict[str, Any] | None:
    """ffprobe's report on a file, or None if it could not read it."""
    try:
        return await _ffprobe(path)
    except (OSError, TimeoutError) as exc:
        log.warning("ffprobe failed: %s", exc)
        return None


def parse(raw: dict[str, Any]) -> MediaInfo:
    streams: list[dict[str, Any]] = raw.get("streams") or []
    fmt: dict[str, Any] = raw.get("format") or {}
    # Cover art in an audio file is reported as a video stream; only a stream
    # that moves makes something a video.
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and (s.get("disposition") or {}).get("attached_pic") != 1
        ),
        None,
    )
    return MediaInfo(
        formats=frozenset(str(fmt.get("format_name") or "").split(",")) - {""},
        video_codec=None if video is None else str(video.get("codec_name") or "unknown"),
        audio_codecs=tuple(
            str(s.get("codec_name") or "unknown") for s in streams if s.get("codec_type") == "audio"
        ),
        duration_s=_number(fmt.get("duration")),
        width=int(_number((video or {}).get("width"))),
        height=int(_number((video or {}).get("height"))),
    )


def _number(value: Any) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        return 0.0
    return number if number > 0 else 0.0


async def _ffprobe(path: str) -> dict[str, Any] | None:
    command = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    process = await _spawn([*command, path], max_file_bytes=0, stdout=asyncio.subprocess.PIPE)
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        (stdout, too_long), (stderr, _) = await asyncio.wait_for(
            asyncio.gather(
                _collect(process, process.stdout, PROBE_OUTPUT_LIMIT_BYTES),
                _collect(process, process.stderr, STDERR_LIMIT_BYTES),
            ),
            PROBE_TIMEOUT_S,
        )
        await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if too_long:
        log.info("ffprobe said too much about %s; treating it as unreadable", path)
        return None
    if process.returncode != 0:
        log.info("ffprobe rejected %s: %s", path, _tail(stderr))
        return None
    try:
        parsed: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return parsed


# ---------------------------------------------------------------- remux


async def remux(
    source: str, target: str, info: MediaInfo, on_progress: ProgressCallback | None = None
) -> bool:
    """Copy the streams of `source` into an MP4 at `target`.

    The video is never re-encoded. Audio is copied when MP4 can carry it and
    converted to AAC otherwise. Subtitles are dropped: MP4 cannot hold most
    subtitle formats and Telegram shows none of them.
    """
    if os.path.realpath(source) == os.path.realpath(target):
        # ffmpeg would refuse, and the cleanup below would then delete the
        # input. Nothing should ask for this; if something does, say no.
        log.error("refusing to remux %s onto itself", source)
        return False
    if not has_room_for(source, info):
        log.warning("not enough free disk to remux %s", source)
        return False
    audio = "copy" if info.audio_fits else "aac"
    # fmt: off
    command = [
        "ffmpeg", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
        "-threads", str(FFMPEG_THREADS), "-i", source,
        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
        "-c:v", "copy", "-c:a", audio,
        *(["-b:a", str(AAC_BITRATE)] if audio == "aac" else []),
        # The index goes at the front, so playback can start before the whole
        # file has arrived. That is what "streamable" means.
        "-movflags", "+faststart",
        target,
    ]
    # fmt: on
    process = await _spawn(
        command,
        max_file_bytes=remux_bound(os.path.getsize(source), info),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        stderr = await asyncio.wait_for(
            _pump(process, int(info.duration_s), on_progress), REMUX_TIMEOUT_S
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _discard(target)
        return False
    except BaseException:
        # Cancelled, or the progress hook failed because whoever asked has
        # gone. Either way ffmpeg must not be left running on its own.
        process.kill()
        await process.wait()
        _discard(target)
        raise
    if process.returncode != 0 or not os.path.exists(target):
        log.info("remux of %s failed: %s", source, _tail(stderr))
        _discard(target)
        return False
    return True


async def _pump(
    process: asyncio.subprocess.Process, total_s: int, on_progress: ProgressCallback | None
) -> bytes:
    """Read ffmpeg's progress lines while draining stderr.

    Both pipes are read at once: reading one while the other fills up its
    buffer deadlocks on exactly the long files progress is for.
    """
    tail: list[bytes] = []

    async def drain_stderr() -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            tail.append(chunk)
            del tail[:-4]

    async def read_progress() -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            seconds = progress_seconds(line)
            if seconds is not None and on_progress is not None:
                await on_progress(seconds, total_s)

    await asyncio.gather(read_progress(), drain_stderr())
    await process.wait()
    return b"".join(tail)


def remux_bound(source_size: int, info: MediaInfo) -> int:
    """The most a remux of this file can legitimately write, in bytes.

    Copied streams come out about the size they went in. Converted audio is
    the exception: a long film with low-bitrate audio can grow a lot once that
    audio is AAC, so its full size at `AAC_BITRATE` is allowed on top. Past
    this, ffmpeg is doing something other than a remux and is stopped before
    it fills the disk.
    """
    bound = source_size * 11 // 10 + 16 * 1024**2
    if not info.audio_fits:
        bound += int(info.duration_s * AAC_BITRATE / 8) * len(info.audio_codecs)
    return bound


def progress_seconds(line: bytes) -> int | None:
    """`out_time_us=12500000` -> 12. Every other key is ignored.

    `out_time_ms` is also microseconds despite its name, so the unambiguous key
    is the one read.
    """
    key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
    if key != "out_time_us":
        return None
    try:
        return max(0, int(value) // 1_000_000)
    except ValueError:
        return None


def has_room_for(path: str, info: MediaInfo) -> bool:
    """A remux writes a second copy next to the first."""
    try:
        needed = remux_bound(os.path.getsize(path), info)
        return shutil.disk_usage(os.path.dirname(path) or ".").free > needed
    except OSError:
        return False


# ---------------------------------------------------------------- thumbnails

_FIT = (
    f"scale={THUMBNAIL_SIDE}:{THUMBNAIL_SIDE}:force_original_aspect_ratio=decrease,"
    "scale=trunc(iw/2)*2:trunc(ih/2)*2"
)


async def frame_thumbnail(video: str, target: str, duration_s: float) -> bool:
    """A frame a third of the way in, capped at ten minutes.

    Not the first frame: films open on black, and a black thumbnail looks like
    a broken upload.
    """
    seek = max(1, min(int(duration_s) // 3, 600)) if duration_s >= 2 else 0
    return await _ffmpeg_image("-ss", str(seek), "-i", video, target=target)


async def fit_thumbnail(image: str, target: str) -> bool:
    """Shrink a user's picture to what Telegram accepts as a thumbnail.

    Its size is read from the header before anything decodes it.
    """
    info = await probe(image)
    if info is None or not info.width or not info.height:
        return False
    if info.width * info.height > THUMBNAIL_MAX_PIXELS:
        log.info("refusing a %sx%s thumbnail", info.width, info.height)
        return False
    return await _ffmpeg_image("-i", image, target=target)


async def _ffmpeg_image(*inputs: str, target: str) -> bool:
    # fmt: off
    command = [
        "ffmpeg", "-nostdin", "-y",
        "-threads", str(FFMPEG_THREADS), "-filter_threads", "1", *inputs,
        "-frames:v", "1", "-vf", _FIT, "-q:v", "4", target,
    ]
    # fmt: on
    process = await _spawn(
        command, max_file_bytes=THUMBNAIL_FILE_LIMIT_BYTES, stdout=asyncio.subprocess.DEVNULL
    )
    assert process.stderr is not None
    try:
        stderr, _ = await asyncio.wait_for(
            _collect(process, process.stderr, STDERR_LIMIT_BYTES), THUMBNAIL_TIMEOUT_S
        )
        await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        return False
    if process.returncode != 0 or not os.path.exists(target):
        log.info("thumbnail failed: %s", _tail(stderr))
        return False
    return True


# ---------------------------------------------------------------- running tools


async def _spawn(
    command: list[str], *, max_file_bytes: int, stdout: int
) -> asyncio.subprocess.Process:
    """Start ffmpeg or ffprobe with as little as it needs.

    Its environment holds only PATH, so it doesn't inherit BOT_TOKEN or
    API_HASH. (That alone wouldn't stop compromised code reading the bot's
    environment through /proc; making the bot non-dumpable at startup does.)
    `prlimit` caps its memory and the size of any file it writes, and `choom`
    makes it the first thing the kernel kills if memory runs out, so the bot
    survives a runaway conversion.
    """
    return await asyncio.create_subprocess_exec(
        *limited(command, max_file_bytes),
        stdout=stdout,
        stderr=asyncio.subprocess.PIPE,
        env=tool_environment(),
    )


def tool_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath)}


def limited(command: list[str], max_file_bytes: int) -> list[str]:
    prlimit, choom = shutil.which("prlimit"), shutil.which("choom")
    if prlimit is None or choom is None:
        _warn_unlimited()
        return command
    return [
        prlimit, f"--as={MEMORY_LIMIT_BYTES}", f"--fsize={max_file_bytes}", "--",
        choom, "-n", "1000", "--",
        *command,
    ]  # fmt: skip


@functools.cache
def _warn_unlimited() -> None:
    log.warning("prlimit or choom is missing, so ffmpeg runs without its limits")


async def _collect(
    process: asyncio.subprocess.Process, stream: asyncio.StreamReader, limit: int
) -> tuple[bytes, bool]:
    """Read a pipe to the end, or kill the process once it passes `limit`.

    Returns what was read and whether the limit was hit.
    """
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            process.kill()
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


def _tail(stderr: bytes) -> str:
    return stderr[-300:].decode("utf-8", errors="replace").strip()


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)
