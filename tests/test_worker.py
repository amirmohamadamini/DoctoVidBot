"""The media worker, over a real Unix socket, in this process.

The separation between users and containers is Docker's job and can't be
tested here; the protocol, the path checks and the bot's distrust of the
answers can.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from doctovid import media
from doctovid.config import load
from doctovid.runner import Runner
from doctovid.store import Store
from doctovid.tools import LocalTools, RemoteTools, WorkerUnavailable
from doctovid.worker import serve

from .conftest import FakeTransport, MakeClip, make_job, make_request, needs_ffmpeg


@pytest.fixture
def socket_path() -> Iterator[Path]:
    # Unix socket paths are limited to about 100 characters, which pytest's
    # own temporary directories can exceed.
    directory = Path(tempfile.mkdtemp(prefix="dv"))
    yield directory / "media.sock"
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    path = tmp_path / "scratch"
    path.mkdir()
    return path


@pytest.fixture
async def remote(socket_path: Path, scratch: Path) -> AsyncIterator[RemoteTools]:
    server = asyncio.create_task(serve(socket_path, scratch, handle_signals=False))
    for _ in range(100):
        if socket_path.exists():
            break
        await asyncio.sleep(0.01)
    yield RemoteTools(socket_path, connect_wait_s=1)
    server.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server


def into(scratch: Path, clip: Path) -> Path:
    job = scratch / "job1-abc"
    job.mkdir(exist_ok=True)
    return Path(shutil.copy(clip, job / "source"))


@needs_ffmpeg
async def test_probe_gives_the_same_answer_as_in_process(
    remote: RemoteTools, scratch: Path, make_clip: MakeClip
) -> None:
    source = into(scratch, make_clip("film.mkv", audio="ac3"))
    assert await remote.probe(source) == await LocalTools().probe(source)


@needs_ffmpeg
async def test_a_remux_reports_progress_and_produces_a_playable_file(
    remote: RemoteTools, scratch: Path, make_clip: MakeClip
) -> None:
    source = into(scratch, make_clip("film.mkv", audio="ac3"))
    info = await remote.probe(source)
    assert info is not None
    seen: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        seen.append((done, total))

    target = source.with_name("playable.mp4")
    assert await remote.remux(source, target, info, on_progress)
    assert seen
    result = await media.probe(str(target))
    assert result is not None
    assert result.plan is media.Plan.READY


@needs_ffmpeg
async def test_thumbnails(remote: RemoteTools, scratch: Path, make_clip: MakeClip) -> None:
    source = into(scratch, make_clip("film.mp4"))
    frame = source.with_name("frame.jpg")
    assert await remote.frame_thumbnail(source, frame, 2.0)
    fitted = source.with_name("fitted.jpg")
    assert await remote.fit_thumbnail(frame, fitted)
    assert fitted.stat().st_size > 0


async def test_paths_outside_scratch_are_refused(
    remote: RemoteTools, scratch: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "bot.session"
    outside.write_bytes(b"auth key")
    with pytest.raises(WorkerUnavailable, match="outside the scratch"):
        await remote.probe(outside)
    with pytest.raises(WorkerUnavailable, match="outside the scratch"):
        await remote.probe(scratch / ".." / "bot.session")

    # A link inside scratch that leads out of it is outside, too.
    (scratch / "sneaky").symlink_to(outside)
    with pytest.raises(WorkerUnavailable, match="outside the scratch"):
        await remote.probe(scratch / "sneaky")


async def test_no_worker_is_reported_rather_than_hanging(tmp_path: Path) -> None:
    tools = RemoteTools(tmp_path / "nobody.sock", connect_wait_s=0.2)
    with pytest.raises(WorkerUnavailable, match="no media worker"):
        await tools.probe(tmp_path / "x")


async def fake_worker(socket_path: Path, *lines: object) -> asyncio.Server:
    """A worker that answers every request with `lines`, whatever was asked."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        for line in lines:
            writer.write((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(answer, path=str(socket_path))


@pytest.mark.parametrize(
    "lines",
    [
        [{"ok": True, "result": "yes"}],
        [{"progress": ["lots", 1]}, {"ok": True, "result": True}],
        [[1, 2, 3]],
        [b"not json"],
        [],
    ],
)
async def test_nonsense_from_the_worker_is_an_error(
    socket_path: Path, tmp_path: Path, lines: list[object]
) -> None:
    server = await fake_worker(socket_path, *lines)
    info = media.MediaInfo(frozenset({"matroska"}), "h264", ("aac",), 2.0)

    async def ignore(done: int, total: int) -> None:
        pass

    async with server:
        with pytest.raises(WorkerUnavailable):
            await RemoteTools(socket_path).remux(tmp_path / "a", tmp_path / "b", info, ignore)


async def test_a_probe_that_isnt_a_report_is_an_error(socket_path: Path, tmp_path: Path) -> None:
    async with await fake_worker(socket_path, {"ok": True, "result": ["not", "a", "dict"]}):
        with pytest.raises(WorkerUnavailable):
            await RemoteTools(socket_path).probe(tmp_path / "a")


@needs_ffmpeg
async def test_a_whole_job_through_the_worker(
    remote: RemoteTools, scratch: Path, tmp_path: Path, store: Store, make_clip: MakeClip
) -> None:
    config = load(
        {
            "API_ID": "1",
            "API_HASH": "h",
            "BOT_TOKEN": "t",
            "DATA_DIR": str(tmp_path / "data"),
            "SCRATCH_DIR": str(scratch),
        }
    )
    telegram = FakeTransport(sent_files={100: make_clip("film.mkv", audio="ac3")})

    await Runner(config, store, telegram, tools=remote)(make_job(make_request("film.mkv")))

    [upload] = telegram.uploads
    assert upload.name == "film.mp4"
    assert upload.video is not None
    assert upload.thumbnail
    assert list(scratch.iterdir()) == []
