from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import FloodWaitError

from doctovid import media
from doctovid.config import Config
from doctovid.i18n import RLM
from doctovid.jobs import Mode
from doctovid.quota import Calendar, Usage
from doctovid.runner import MIN_DISK_MARGIN_BYTES, Runner, job_deadline_s, usable_disk
from doctovid.store import Store
from doctovid.tools import LocalTools

from .conftest import (
    CHANNEL,
    FA,
    FakeTransport,
    MakeClip,
    bare,
    make_job,
    make_request,
    needs_ffmpeg,
)


@pytest.fixture
def telegram() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def runner(config: Config, store: Store, telegram: FakeTransport) -> Runner:
    return Runner(config, store, telegram)


def forbid_remux(monkeypatch: pytest.MonkeyPatch) -> None:
    async def remux(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("this video should not have been remuxed")

    monkeypatch.setattr(media, "remux", remux)


@needs_ffmpeg
async def test_an_mkv_comes_back_as_a_playable_mp4(
    runner: Runner, telegram: FakeTransport, make_clip: MakeClip
) -> None:
    telegram.sent_files[100] = make_clip("film.mkv", audio="ac3")
    await runner(make_job(make_request("film.mkv")))

    [upload] = telegram.uploads
    assert upload.name == "film.mp4"
    assert upload.video is not None
    assert upload.video.duration_s > 0
    assert upload.thumbnail is not None
    assert telegram.deleted == [101]


@needs_ffmpeg
async def test_a_playable_mp4_is_sent_as_it_is(
    runner: Runner,
    telegram: FakeTransport,
    make_clip: MakeClip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip = make_clip("ready.mp4", faststart=True)
    telegram.sent_files[100] = clip
    forbid_remux(monkeypatch)

    await runner(make_job(make_request("ready.mp4")))

    [upload] = telegram.uploads
    assert upload.content == clip.read_bytes()
    assert upload.video is not None


@needs_ffmpeg
async def test_send_as_file_never_touches_the_bytes(
    runner: Runner,
    telegram: FakeTransport,
    make_clip: MakeClip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip = make_clip("film.mkv")
    telegram.sent_files[100] = clip
    forbid_remux(monkeypatch)
    request = make_request("film.mkv", new_name="renamed.mkv")

    await runner(make_job(request, mode=Mode.FILE))

    [upload] = telegram.uploads
    assert upload.name == "renamed.mkv"
    assert upload.video is None
    assert upload.content == clip.read_bytes()


@needs_ffmpeg
async def test_a_video_that_needs_reencoding_is_refused(
    runner: Runner, telegram: FakeTransport, make_clip: MakeClip
) -> None:
    telegram.sent_files[100] = make_clip("film.webm", video="libvpx-vp9", audio=None)
    await runner(make_job(make_request("film.webm")))

    assert telegram.uploads == []
    assert "vp9" in telegram.edits[-1]


async def test_a_file_deleted_from_the_chat(runner: Runner, telegram: FakeTransport) -> None:
    await runner(make_job(make_request("gone.mkv")))
    assert telegram.uploads == []
    assert "no longer in this chat" in telegram.edits[-1]


async def test_a_hostile_name_stays_in_the_work_directory(
    runner: Runner, telegram: FakeTransport, config: Config, tmp_path: Path
) -> None:
    source = tmp_path / "payload"
    source.write_bytes(b"data")
    telegram.sent_files[100] = source

    await runner(make_job(make_request("../../escaped.txt", is_video=False), mode=Mode.FILE))

    assert not (config.work_dir.parent / "escaped.txt").exists()
    assert telegram.uploads[0].content == b"data"


async def test_the_work_directory_is_cleaned_up(
    runner: Runner, telegram: FakeTransport, config: Config, tmp_path: Path
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")
    telegram.sent_files[100] = source
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert list(config.work_dir.iterdir()) == []


# ---------------------------------------------------------------- cache


@pytest.fixture
def pdf(telegram: FakeTransport, tmp_path: Path) -> Path:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")
    telegram.sent_files[100] = source
    return source


@pytest.fixture
def movie(telegram: FakeTransport, make_clip: MakeClip) -> Path:
    clip = make_clip("movie.mp4")
    telegram.sent_files[100] = clip
    return clip


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_playable_video_is_cached_and_served_again(
    runner: Runner, telegram: FakeTransport, store: Store, config: Config
) -> None:
    store.set_cache_enabled(True)

    await runner(make_job(make_request("movie.mp4")))
    assert len(telegram.uploads) == 1
    delivered = telegram.next_id - 1
    assert telegram.copies == [(42, delivered, config.cache_channel_id)]

    await runner(make_job(make_request("movie.mp4", user_id=43)))
    assert len(telegram.uploads) == 1
    assert telegram.copies[-1] == (config.cache_channel_id, store.cached(CHANNEL, "555:video"), 43)


@pytest.mark.usefixtures("movie")
async def test_a_video_sent_back_as_a_file_is_cached_too(
    runner: Runner, telegram: FakeTransport, store: Store, config: Config
) -> None:
    store.set_cache_enabled(True)

    await runner(make_job(make_request("movie.mp4"), mode=Mode.FILE))
    assert telegram.uploads[0].video is None
    assert store.cached(CHANNEL, "555:file") is not None
    assert store.cached(CHANNEL, "555:video") is None

    await runner(make_job(make_request("movie.mp4", user_id=43), mode=Mode.FILE))
    assert len(telegram.uploads) == 1
    assert telegram.copies[-1] == (config.cache_channel_id, store.cached(CHANNEL, "555:file"), 43)


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_cached_file_is_not_handed_out_as_the_playable_video(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    store.set_cache_enabled(True)
    store.remember(CHANNEL, "555:file", 77)

    await runner(make_job(make_request("movie.mp4")))
    assert len(telegram.uploads) == 1
    assert telegram.uploads[0].video is not None
    assert all(message_id != 77 for _, message_id, _ in telegram.copies)


@pytest.mark.usefixtures("pdf")
async def test_a_document_sent_back_as_it_is_is_never_cached(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    # Most likely a private document, and re-sending it costs nothing anyway.
    store.set_cache_enabled(True)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert telegram.copies == []
    assert store.cache_size(CHANNEL) == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_nothing_is_cached_while_the_cache_is_off(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    await runner(make_job(make_request("movie.mp4")))
    assert telegram.copies == []
    assert store.cache_size(CHANNEL) == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_renamed_video_is_not_cached(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    store.set_cache_enabled(True)
    await runner(make_job(make_request("movie.mp4", new_name="mine.mp4")))
    assert telegram.copies == []
    assert store.cache_size(CHANNEL) == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_cache_entry_deleted_from_the_channel_is_forgotten(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    store.set_cache_enabled(True)
    store.remember(CHANNEL, "555:video", 77)
    telegram.gone.add(77)

    await runner(make_job(make_request("movie.mp4")))

    assert len(telegram.uploads) == 1
    assert store.cached(CHANNEL, "555:video") != 77


# ---------------------------------------------------------------- failures


@pytest.mark.usefixtures("pdf")
async def test_a_floodwait_tells_the_user_how_long(
    runner: Runner, telegram: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def limited(*args: Any, **kwargs: Any) -> int:
        raise FloodWaitError(request=None, capture=90)

    monkeypatch.setattr(telegram, "upload", limited)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert "Try again in 2 min." in telegram.edits[-1]


async def test_a_transfer_that_never_ends_is_given_up(
    config: Config, store: Store, telegram: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stuck(*args: Any, **kwargs: Any) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(telegram, "download", stuck)
    runner = Runner(config, store, telegram, deadline_s=lambda _size: 0.05)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))

    assert "took far too long" in telegram.edits[-1]
    assert runner._reserved == 0
    assert store.usage(42, Calendar("UTC").today())[0] == Usage()


def test_a_bigger_file_gets_longer_but_never_forever() -> None:
    assert 3600 < job_deadline_s(0) < job_deadline_s(2 * 1024**3) < float("inf")


@pytest.mark.usefixtures("pdf")
async def test_a_job_that_would_fill_the_disk_is_refused(
    config: Config, store: Store, telegram: FakeTransport
) -> None:
    runner = Runner(config, store, telegram, usable_bytes=lambda _: 500)
    request = make_request("doc.pdf", is_video=False, size=1000)

    await runner(make_job(request, mode=Mode.FILE))

    assert telegram.uploads == []
    assert "disk space" in telegram.edits[-1]


@needs_ffmpeg
async def test_a_remux_reserves_room_for_what_it_may_write(
    config: Config, store: Store, make_clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Room for the download, not for a remux whose audio is converted.
    clip = make_clip("film.mkv", audio="mp2")
    size = clip.stat().st_size
    telegram = FakeTransport(sent_files={100: clip})
    runner = Runner(config, store, telegram, usable_bytes=lambda _: size * 3)
    forbid_remux(monkeypatch)

    await runner(make_job(make_request("film.mkv", size=size)))

    assert "disk space" in telegram.edits[-1]
    assert runner._reserved == 0


def test_a_tenth_of_the_disk_stays_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gb = 1024**3

    def disk(total: int, free: int) -> None:
        usage = SimpleNamespace(total=total, used=total - free, free=free)
        monkeypatch.setattr(shutil, "disk_usage", lambda _: usage)

    disk(total=100 * gb, free=30 * gb)
    assert usable_disk(tmp_path) == 20 * gb
    disk(total=5 * gb, free=3 * gb)
    assert usable_disk(tmp_path) == 3 * gb - MIN_DISK_MARGIN_BYTES


# ---------------------------------------------------------------- usage


@pytest.mark.usefixtures("pdf")
async def test_a_delivered_file_is_counted(runner: Runner, store: Store) -> None:
    await runner(make_job(make_request("doc.pdf", is_video=False, size=4), mode=Mode.FILE))
    today, _ = store.usage(42, Calendar("UTC").today())
    assert today == Usage(1, 4)


async def test_a_failed_file_is_not_counted(runner: Runner, store: Store) -> None:
    await runner(make_job(make_request("gone.pdf", is_video=False), mode=Mode.FILE))
    today, _ = store.usage(42, Calendar("UTC").today())
    assert today == Usage()


async def test_a_file_from_the_cache_is_counted(runner: Runner, store: Store) -> None:
    store.set_cache_enabled(True)
    store.remember(CHANNEL, "555:video", 70)
    await runner(make_job(make_request("movie.mp4")))
    today, _ = store.usage(42, Calendar("UTC").today())
    assert today.files == 1


# ---------------------------------------------------------------- names


@needs_ffmpeg
@pytest.mark.parametrize("name", ["playable.mp4", "frame.jpg", "PLAYABLE.MP4", "input"])
async def test_a_file_named_like_an_internal_file_still_works(
    runner: Runner, telegram: FakeTransport, make_clip: MakeClip, name: str
) -> None:
    # An MKV under a name the runner uses for its own files: the download and
    # the remux output must not be the same path.
    telegram.sent_files[100] = make_clip("film.mkv", audio="ac3")
    await runner(make_job(make_request(name)))

    [upload] = telegram.uploads
    assert upload.video is not None
    assert upload.name.endswith(".mp4")


@needs_ffmpeg
async def test_remux_refuses_to_write_over_its_input(make_clip: MakeClip) -> None:
    clip = make_clip("film.mkv")
    info = await media.probe(str(clip))
    assert info is not None
    assert not await media.remux(str(clip), str(clip), info)
    assert clip.exists()


# ---------------------------------------------------------------- a compromised worker


class PlantingTools(LocalTools):
    """A media worker that has been taken over: instead of a video, it leaves a
    link to the bot's session where the result should be."""

    def __init__(self, secret: Path) -> None:
        self.secret = secret

    async def remux(
        self, source: Path, target: Path, info: media.MediaInfo, on_progress: Any
    ) -> bool:
        target.symlink_to(self.secret)
        return True

    async def frame_thumbnail(self, video: Path, target: Path, duration_s: float) -> bool:
        target.symlink_to(self.secret)
        return True


@needs_ffmpeg
async def test_a_compromised_worker_cannot_get_the_session_uploaded(
    config: Config, store: Store, telegram: FakeTransport, make_clip: MakeClip, tmp_path: Path
) -> None:
    secret = tmp_path / "bot.session"
    secret.write_bytes(b"the bot's auth key")
    runner = Runner(config, store, telegram, tools=PlantingTools(secret))
    telegram.sent_files[100] = make_clip("film.mkv", audio="ac3")

    await runner(make_job(make_request("film.mkv")))

    assert all(b"auth key" not in upload.content for upload in telegram.uploads)
    assert telegram.uploads == []
    assert "Something went wrong preparing that file" in telegram.edits[-1]


@pytest.mark.usefixtures("pdf")
async def test_a_persian_speaker_is_answered_in_persian(
    config: Config, store: Store, telegram: FakeTransport
) -> None:
    runner = Runner(config, store, telegram, locale_for=lambda _user_id: FA)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert bare(telegram.edits[0]) == "⬇️ در حال دریافت <b>doc.pdf</b>…"
    assert telegram.edits[0].startswith(RLM)


async def test_a_failure_is_explained_in_persian(
    config: Config, store: Store, telegram: FakeTransport
) -> None:
    runner = Runner(config, store, telegram, locale_for=lambda _user_id: FA)
    await runner(make_job(make_request("gone.pdf", is_video=False), mode=Mode.FILE))
    assert bare(telegram.edits[-1]) == "⚠️ " + bare(FA.phrase("job.gone"))


@pytest.mark.usefixtures("movie")
async def test_a_file_replaced_after_the_menu_is_refused(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    # Someone sends a small video, then swaps a large one into the message.
    store.set_cache_enabled(True)
    telegram.replaced.add(100)
    await runner(make_job(make_request("movie.mp4"), mode=Mode.FILE))

    assert telegram.uploads == []
    assert store.cache_size(CHANNEL) == 0
    assert "different file" in telegram.edits[-1]
    assert store.usage(42, Calendar("UTC").today())[0] == Usage()


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_new_cache_channel_starts_empty(
    config: Config, store: Store, telegram: FakeTransport
) -> None:
    # Message 27 of the old channel is some unrelated message in the new one.
    store.set_cache_enabled(True)
    store.remember(CHANNEL, "555:video", 27)
    moved = replace(config, cache_channel_id=-1009999)

    await Runner(moved, store, telegram)(make_job(make_request("movie.mp4")))

    assert all(message_id != 27 for _, message_id, _ in telegram.copies)
    assert len(telegram.uploads) == 1
    assert store.cached(-1009999, "555:video") is not None


async def test_a_cached_delivery_is_counted_even_if_cancelled_after(
    config: Config, store: Store
) -> None:
    store.set_cache_enabled(True)
    store.remember(CHANNEL, "555:video", 27)
    deleting = asyncio.Event()

    class SlowDelete(FakeTransport):
        async def delete(self, chat_id: int, message_id: int) -> None:
            deleting.set()
            await asyncio.Event().wait()

    telegram = SlowDelete()
    runner = Runner(config, store, telegram)
    task = asyncio.create_task(runner(make_job()))
    await deleting.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(telegram.copies) == 1
    assert store.usage(42, Calendar("UTC").today())[0].files == 1


@needs_ffmpeg
async def test_an_mp4_indexed_last_comes_back_streamable(
    runner: Runner, telegram: FakeTransport, make_clip: MakeClip
) -> None:
    clip = make_clip("camera.mp4")
    telegram.sent_files[100] = clip
    await runner(make_job(make_request("camera.mp4")))

    [upload] = telegram.uploads
    assert upload.content.index(b"moov") < upload.content.index(b"mdat")
    assert upload.name == "camera.mp4"


@needs_ffmpeg
async def test_a_finished_download_is_not_counted_twice(
    config: Config, store: Store, make_clip: MakeClip
) -> None:
    # Room for the download and its remux, and no more. Once downloaded, the
    # file is in the free space figure: it mustn't also count as reserved.
    clip = make_clip("film.mkv", audio="aac")
    size = clip.stat().st_size
    info = await media.probe(str(clip))
    assert info is not None
    usable = [size + media.remux_bound(size, info)]

    class DiskFills(FakeTransport):
        async def download(self, *args: Any, **kwargs: Any) -> bool:
            found = await super().download(*args, **kwargs)
            usable[0] -= size
            return found

    telegram = DiskFills(sent_files={100: clip})
    runner = Runner(config, store, telegram, usable_bytes=lambda _: usable[0])
    await runner(make_job(make_request("film.mkv", size=size)))

    assert len(telegram.uploads) == 1
    assert runner._reserved == 0
