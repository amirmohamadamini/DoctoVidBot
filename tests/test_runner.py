from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from telethon.errors import FloodWaitError

from doctovid import media
from doctovid.config import Config
from doctovid.jobs import Mode
from doctovid.quota import Calendar, Usage
from doctovid.runner import DISK_MARGIN_BYTES, Runner
from doctovid.store import Store
from doctovid.tools import LocalTools

from .conftest import FakeTransport, MakeClip, make_job, make_request, needs_ffmpeg


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
    clip = make_clip("ready.mp4")
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
    assert telegram.copies[-1] == (config.cache_channel_id, store.cached("555:video"), 43)


@pytest.mark.usefixtures("pdf")
async def test_a_file_sent_back_as_it_is_is_never_cached(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    # Most likely a private document, and re-sending it costs nothing anyway.
    store.set_cache_enabled(True)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert telegram.copies == []
    assert store.cache_size() == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_nothing_is_cached_while_the_cache_is_off(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    await runner(make_job(make_request("movie.mp4")))
    assert telegram.copies == []
    assert store.cache_size() == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_renamed_video_is_not_cached(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    store.set_cache_enabled(True)
    await runner(make_job(make_request("movie.mp4", new_name="mine.mp4")))
    assert telegram.copies == []
    assert store.cache_size() == 0


@needs_ffmpeg
@pytest.mark.usefixtures("movie")
async def test_a_cache_entry_deleted_from_the_channel_is_forgotten(
    runner: Runner, telegram: FakeTransport, store: Store
) -> None:
    store.set_cache_enabled(True)
    store.remember("555:video", 77)
    telegram.gone.add(77)

    await runner(make_job(make_request("movie.mp4")))

    assert len(telegram.uploads) == 1
    assert store.cached("555:video") != 77


# ---------------------------------------------------------------- failures


@pytest.mark.usefixtures("pdf")
async def test_a_floodwait_tells_the_user_how_long(
    runner: Runner, telegram: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def limited(*args: Any, **kwargs: Any) -> int:
        raise FloodWaitError(request=None, capture=90)

    monkeypatch.setattr(telegram, "upload", limited)
    await runner(make_job(make_request("doc.pdf", is_video=False), mode=Mode.FILE))
    assert "Try again in 1:30" in telegram.edits[-1]


@pytest.mark.usefixtures("pdf")
async def test_a_job_that_would_fill_the_disk_is_refused(
    config: Config, store: Store, telegram: FakeTransport
) -> None:
    runner = Runner(config, store, telegram, free_bytes=lambda _: DISK_MARGIN_BYTES + 500)
    request = make_request("doc.pdf", is_video=False, size=1000)

    await runner(make_job(request, mode=Mode.FILE))

    assert telegram.uploads == []
    assert "disk space" in telegram.edits[-1]


async def test_a_video_reserves_room_for_its_remux(config: Config, store: Store) -> None:
    telegram = FakeTransport()
    runner = Runner(config, store, telegram, free_bytes=lambda _: DISK_MARGIN_BYTES + 1500)
    await runner(make_job(make_request("film.mkv", size=1000), mode=Mode.VIDEO))
    assert "disk space" in telegram.edits[-1]


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
    store.remember("555:video", 70)
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
