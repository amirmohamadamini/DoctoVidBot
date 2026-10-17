"""The media worker: ffmpeg and ffprobe, kept away from everything else.

Run as `doctovid-media` in its own container, as its own user, with no network
and nothing mounted but the scratch volume. It never sees the bot's token, its
Telegram session or its database, so a file crafted to take over ffmpeg gets
none of those.

It listens on a Unix socket on the scratch volume. Each connection carries one
request, a JSON line such as

    {"op": "remux", "source": "/scratch/jobs/…/source", "target": "…/playable.mp4"}

answered by `{"started": true}` once a slot is free, `{"progress": [done,
total]}` lines for a remux, and then `{"ok": true, "result": …}` or
`{"ok": false, "error": "…"}`. If the bot hangs up mid-request, which is what
cancelling a job does, the work is stopped.

It accepts paths only inside the scratch directory. The bot doesn't trust what
comes back either: see `safefile.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import media
from .hardening import make_undumpable
from .tools import LINE_LIMIT

log = logging.getLogger("doctovid.worker")

#: Operations at once. The bot runs `WORKERS` jobs and up to two thumbnails.
DEFAULT_PARALLEL = 4
MAX_PARALLEL = 64

REQUEST_TIMEOUT_S = 10.0


class BadRequest(Exception):
    pass


Send = Callable[[dict[str, Any]], Awaitable[None]]


class Worker:
    def __init__(self, scratch: Path, *, parallel: int = DEFAULT_PARALLEL) -> None:
        # With no slots it would listen, and never start anything.
        if not 1 <= parallel <= MAX_PARALLEL:
            raise ValueError(f"parallel must be between 1 and {MAX_PARALLEL}, got {parallel}")
        self._root = os.path.realpath(scratch)
        self._slots = asyncio.Semaphore(parallel)
        self._active: set[asyncio.Task[Any]] = set()

    async def stop(self) -> None:
        """Stop every request still running, ffmpeg included."""
        for task in self._active:
            task.cancel()
        await asyncio.gather(*self._active, return_exceptions=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(message: dict[str, Any]) -> None:
            writer.write(json.dumps(message).encode() + b"\n")
            await writer.drain()

        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
            request = json.loads(line)
            if not isinstance(request, dict):
                raise BadRequest("not a JSON object")
            result = await self._until_hangup(reader, self._queued(request, send))
            await send({"ok": True, "result": result})
        except (BadRequest, ValueError, KeyError, TypeError) as exc:
            log.warning("refused a request: %s", exc)
            with contextlib.suppress(OSError):
                await send({"ok": False, "error": str(exc) or type(exc).__name__})
        except ConnectionError, TimeoutError:
            log.info("the bot went away mid-request")
        except Exception:
            log.exception("request failed")
            with contextlib.suppress(OSError):
                await send({"ok": False, "error": "internal error"})
        finally:
            self._active.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _queued(self, request: dict[str, Any], send: Send) -> Any:
        async with self._slots:
            # The bot's time limit for the operation starts now, not while it
            # waited here behind other files.
            await send({"started": True})
            return await self._run(request, send)

    async def _until_hangup(self, reader: asyncio.StreamReader, work: Awaitable[Any]) -> Any:
        """Run `work`, and stop it if the bot hangs up first, whether it is
        still waiting for a slot or already running ffmpeg.

        Hanging up is how the bot cancels a job. It sends nothing after its
        request, so anything arriving on the connection means it has closed.
        """
        job = asyncio.ensure_future(work)
        hangup = asyncio.ensure_future(reader.read(1))
        try:
            await asyncio.wait({job, hangup}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not job.done():
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            hangup.cancel()
        if job.cancelled():
            raise ConnectionResetError("the bot hung up")
        return job.result()

    async def _run(self, request: dict[str, Any], send: Send) -> Any:
        match request.get("op"):
            case "probe":
                return await media.run_ffprobe(self._inside(request["path"]))
            case "remux":
                source = self._inside(request["source"])
                target = self._inside(request["target"])
                info = await media.probe(source)
                if info is None:
                    return False

                async def progress(done: int, total: int) -> None:
                    await send({"progress": [done, total]})

                return await media.remux(source, target, info, progress)
            case "frame":
                duration = request["duration"]
                if not isinstance(duration, int | float) or duration < 0:
                    raise BadRequest("duration must be a non-negative number")
                return await media.frame_thumbnail(
                    self._inside(request["video"]), self._inside(request["target"]), duration
                )
            case "fit":
                return await media.fit_thumbnail(
                    self._inside(request["image"]), self._inside(request["target"])
                )
            case other:
                raise BadRequest(f"unknown operation {other!r}")

    def _inside(self, value: Any) -> str:
        """A path the request may use: absolute, and inside the scratch directory
        once every symlink and `..` in it is resolved."""
        if not isinstance(value, str) or not os.path.isabs(value):
            raise BadRequest("paths must be absolute")
        real = os.path.realpath(value)
        if real == self._root or os.path.commonpath([real, self._root]) != self._root:
            raise BadRequest("that path is outside the scratch directory")
        return real


async def serve(
    socket_path: Path,
    scratch: Path,
    *,
    parallel: int = DEFAULT_PARALLEL,
    handle_signals: bool = True,
) -> None:
    worker = Worker(scratch, parallel=parallel)
    socket_path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(worker.handle, path=str(socket_path), limit=LINE_LIMIT)
    # The bot connects through the group both users share; nobody else may.
    socket_path.chmod(0o660)
    if handle_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, server.close)
    log.info("media worker listening on %s", socket_path)
    async with server:
        with contextlib.suppress(asyncio.CancelledError):
            await server.serve_forever()
        await worker.stop()


def main() -> None:
    try:
        parallel = int(os.environ.get("MEDIA_PARALLEL", DEFAULT_PARALLEL))
    except ValueError:
        sys.exit("MEDIA_PARALLEL must be a whole number")
    if not 1 <= parallel <= MAX_PARALLEL:
        sys.exit(f"MEDIA_PARALLEL must be between 1 and {MAX_PARALLEL}, got {parallel}")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not make_undumpable():
        log.warning("could not make the worker undumpable")
    # Files it writes are readable by the bot, through the shared group, and
    # by nobody else.
    os.umask(0o027)
    socket_path = Path(os.environ.get("MEDIA_SOCKET", "/scratch/sock/media.sock"))
    scratch = Path(os.environ.get("SCRATCH_DIR", "/scratch/jobs"))
    asyncio.run(serve(socket_path, scratch, parallel=parallel))


if __name__ == "__main__":
    main()
