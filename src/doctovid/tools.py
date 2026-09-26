"""Where ffmpeg runs: in the media container, or in-process for local use.

In production ffmpeg runs in its own container (`doctovid-media`, see
`worker.py`), as another user, with no network and no access to the bot's
data. The bot asks it for things over a Unix socket on the scratch volume the
two share: one JSON request per connection, answered by progress lines and a
final result.

Run straight from a checkout, without that container, the same operations
happen in-process. That is fine for trying the bot out and for the tests, and
is logged as a warning so nobody runs it that way in production by accident.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from . import media
from .progress import ProgressCallback

log = logging.getLogger(__name__)

#: The longest line either side will read. An ffprobe report is capped at
#: `media.PROBE_OUTPUT_LIMIT_BYTES`, and this leaves room for its JSON framing.
LINE_LIMIT = 2 * media.PROBE_OUTPUT_LIMIT_BYTES

#: How long the bot keeps trying to reach a worker that isn't up yet, for
#: example just after both containers start.
CONNECT_WAIT_S = 20.0

#: Each operation's own time limit, plus a margin: the worker enforces the
#: limit, this is only for a worker that has stopped answering altogether.
TIMEOUTS_S = {
    "probe": media.PROBE_TIMEOUT_S + 15,
    "remux": media.REMUX_TIMEOUT_S + 60,
    "frame": media.THUMBNAIL_TIMEOUT_S + 15,
    "fit": media.PROBE_TIMEOUT_S + media.THUMBNAIL_TIMEOUT_S + 15,
}


class WorkerUnavailable(Exception):
    """The media worker can't be reached, or answered nonsense."""


class MediaTools(Protocol):
    async def probe(self, path: Path) -> media.MediaInfo | None: ...

    async def remux(
        self, source: Path, target: Path, info: media.MediaInfo, on_progress: ProgressCallback
    ) -> bool: ...

    async def frame_thumbnail(self, video: Path, target: Path, duration_s: float) -> bool: ...

    async def fit_thumbnail(self, image: Path, target: Path) -> bool: ...

    def share_dir(self, path: Path) -> None:
        """Let the worker write in a directory the bot created."""

    def share_file(self, path: Path) -> None:
        """Let the worker read a file the bot wrote."""


class LocalTools:
    """ffmpeg in this process. For local use and tests, not for production."""

    async def probe(self, path: Path) -> media.MediaInfo | None:
        return await media.probe(str(path))

    async def remux(
        self, source: Path, target: Path, info: media.MediaInfo, on_progress: ProgressCallback
    ) -> bool:
        return await media.remux(str(source), str(target), info, on_progress)

    async def frame_thumbnail(self, video: Path, target: Path, duration_s: float) -> bool:
        return await media.frame_thumbnail(str(video), str(target), duration_s)

    async def fit_thumbnail(self, image: Path, target: Path) -> bool:
        return await media.fit_thumbnail(str(image), str(target))

    def share_dir(self, path: Path) -> None:
        pass

    def share_file(self, path: Path) -> None:
        pass


class RemoteTools:
    """The media worker, over its socket."""

    def __init__(self, socket_path: Path, *, connect_wait_s: float = CONNECT_WAIT_S) -> None:
        self._socket = socket_path
        self._connect_wait_s = connect_wait_s

    async def probe(self, path: Path) -> media.MediaInfo | None:
        raw = await self._call({"op": "probe", "path": str(path)})
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise WorkerUnavailable("the worker's probe wasn't a JSON object")
        try:
            return media.parse(raw)
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorkerUnavailable("the worker's probe didn't make sense") from exc

    async def remux(
        self, source: Path, target: Path, info: media.MediaInfo, on_progress: ProgressCallback
    ) -> bool:
        request = {"op": "remux", "source": str(source), "target": str(target)}
        return _boolean(await self._call(request, on_progress))

    async def frame_thumbnail(self, video: Path, target: Path, duration_s: float) -> bool:
        request = {"op": "frame", "video": str(video), "target": str(target)}
        return _boolean(await self._call({**request, "duration": duration_s}))

    async def fit_thumbnail(self, image: Path, target: Path) -> bool:
        return _boolean(await self._call({"op": "fit", "image": str(image), "target": str(target)}))

    def share_dir(self, path: Path) -> None:
        # Group scratch, which both users belong to; the setgid bit makes what
        # the worker creates inside belong to that group too, so the bot can
        # read it back.
        path.chmod(0o2770)

    def share_file(self, path: Path) -> None:
        path.chmod(0o640)

    async def _call(
        self, request: dict[str, Any], on_progress: ProgressCallback | None = None
    ) -> Any:
        reader, writer = await self._connect()
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            async with asyncio.timeout(TIMEOUTS_S[request["op"]]):
                return await _read_reply(reader, on_progress)
        except TimeoutError as exc:
            raise WorkerUnavailable(f"the worker stopped answering a {request['op']}") from exc
        except (OSError, ValueError) as exc:
            raise WorkerUnavailable(f"talking to the worker failed: {exc}") from exc
        finally:
            # Also how a cancel reaches the worker: it sees the connection
            # close and stops ffmpeg.
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        deadline = asyncio.get_running_loop().time() + self._connect_wait_s
        while True:
            try:
                return await asyncio.open_unix_connection(str(self._socket), limit=LINE_LIMIT)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise WorkerUnavailable(f"no media worker at {self._socket}") from exc
                await asyncio.sleep(0.5)


async def _read_reply(reader: asyncio.StreamReader, on_progress: ProgressCallback | None) -> Any:
    while line := await reader.readline():
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("a reply line wasn't a JSON object")
        if "progress" in message:
            done, total = _progress(message["progress"])
            if on_progress is not None:
                await on_progress(done, total)
            continue
        if message.get("ok") is True:
            return message.get("result")
        raise ValueError(f"the worker refused: {message.get('error', 'no reason given')}")
    raise ValueError("the worker hung up without an answer")


def _progress(value: Any) -> tuple[int, int]:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in value)
    ):
        return value[0], value[1]
    raise ValueError("a progress line didn't make sense")


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise WorkerUnavailable("the worker's answer wasn't yes or no")
    return value


def tools_for(socket_path: Path | None) -> MediaTools:
    if socket_path is None:
        log.warning(
            "MEDIA_SOCKET isn't set, so ffmpeg runs inside the bot process. Fine for trying "
            "the bot out; in production run it with docker compose, which isolates ffmpeg."
        )
        return LocalTools()
    return RemoteTools(socket_path)


__all__ = [
    "LocalTools",
    "MediaTools",
    "RemoteTools",
    "WorkerUnavailable",
    "tools_for",
]
