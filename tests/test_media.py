from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from doctovid import media
from doctovid.media import Plan

from .conftest import MakeClip, needs_ffmpeg

needs_prlimit = pytest.mark.skipif(shutil.which("prlimit") is None, reason="no prlimit")


def probed(format_name: str, video: str | None, *audio: str) -> dict[str, Any]:
    streams: list[dict[str, Any]] = []
    if video is not None:
        streams.append({"codec_type": "video", "codec_name": video, "width": 640, "height": 360})
    streams += [{"codec_type": "audio", "codec_name": codec} for codec in audio]
    return {"format": {"format_name": format_name, "duration": "12.5"}, "streams": streams}


MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


@pytest.mark.parametrize(
    ("raw", "plan"),
    [
        (probed(MP4, "h264", "aac"), Plan.READY),
        (probed(MP4, "hevc"), Plan.READY),
        (probed(MP4, "h264", "ac3"), Plan.REMUX),
        (probed("matroska,webm", "h264", "aac"), Plan.REMUX),
        (probed("matroska,webm", "h264", "dts", "ac3"), Plan.REMUX),
        (probed("avi", "mpeg4", "mp3"), Plan.REMUX),
        (probed("matroska,webm", "vp9", "opus"), Plan.UNSUPPORTED),
        (probed(MP4, "mpeg2video", "aac"), Plan.UNSUPPORTED),
    ],
)
def test_plan(raw: dict[str, Any], plan: Plan) -> None:
    assert media.parse(raw).plan is plan


def test_cover_art_does_not_make_audio_a_video() -> None:
    raw = probed("mp3", None, "mp3")
    raw["streams"].append(
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}
    )
    assert not media.parse(raw).is_video


def test_parse_reads_duration_and_size() -> None:
    info = media.parse(probed(MP4, "h264", "aac"))
    assert (info.duration_s, info.width, info.height) == (12.5, 640, 360)


@pytest.mark.parametrize(
    ("line", "seconds"),
    [
        (b"out_time_us=12500000\n", 12),
        (b"out_time_ms=12500000\n", None),
        (b"out_time_us=N/A\n", None),
        (b"progress=continue\n", None),
    ],
)
def test_progress_seconds(line: bytes, seconds: int | None) -> None:
    assert media.progress_seconds(line) == seconds


# ---------------------------------------------------------------- with ffmpeg


@needs_ffmpeg
async def test_an_mkv_is_remuxed_into_a_playable_mp4(make_clip: MakeClip, tmp_path: Path) -> None:
    source = make_clip("film.mkv", video="mpeg4", audio="ac3")
    info = await media.probe(str(source))
    assert info is not None
    assert info.plan is Plan.REMUX

    seen: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        seen.append((done, total))

    target = tmp_path / "film.mp4"
    assert await media.remux(str(source), str(target), info, on_progress)

    result = await media.probe(str(target))
    assert result is not None
    assert result.plan is Plan.READY
    assert result.audio_codecs == ("aac",)
    assert seen
    assert seen[-1][1] == 2


@needs_ffmpeg
async def test_a_playable_mp4_needs_nothing(make_clip: MakeClip) -> None:
    info = await media.probe(str(make_clip("ready.mp4", video="mpeg4", audio="aac")))
    assert info is not None
    assert info.plan is Plan.READY


@needs_ffmpeg
async def test_an_unreadable_file_probes_as_none(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video at all")
    assert await media.probe(str(junk)) is None


@needs_ffmpeg
async def test_thumbnails_fit_telegrams_limits(make_clip: MakeClip, tmp_path: Path) -> None:
    clip = make_clip("wide.mp4")
    frame = tmp_path / "frame.jpg"
    assert await media.frame_thumbnail(str(clip), str(frame), 2.0)

    fitted = tmp_path / "fitted.jpg"
    assert await media.fit_thumbnail(str(frame), str(fitted))
    info = await media.probe(str(fitted))
    assert info is not None
    assert max(info.width, info.height) <= media.THUMBNAIL_SIDE
    assert fitted.stat().st_size < 200 * 1024


@pytest.fixture
def pixel_bomb(tmp_path: Path) -> Path:
    """7000 x 7000 of one colour: a few kilobytes of PNG, 49 megapixels decoded."""
    bomb = tmp_path / "bomb.png"
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi"]
    command += ["-i", "color=c=red:s=7000x7000", "-frames:v", "1", str(bomb)]
    subprocess.run(command, check=True)
    return bomb


@needs_ffmpeg
async def test_a_huge_picture_is_refused_before_decoding(pixel_bomb: Path, tmp_path: Path) -> None:
    assert pixel_bomb.stat().st_size < 1024 * 1024
    assert not await media.fit_thumbnail(str(pixel_bomb), str(tmp_path / "out.jpg"))
    assert not (tmp_path / "out.jpg").exists()


# ---------------------------------------------------------------- limits


async def run_limited(script: str, max_file_bytes: int = 0) -> bytes:
    process = await media._spawn(
        ["sh", "-c", script], max_file_bytes=max_file_bytes, stdout=asyncio.subprocess.PIPE
    )
    out, _ = await process.communicate()
    return out.strip()


async def test_tools_never_see_the_bots_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:secret")
    monkeypatch.setenv("API_HASH", "secret")
    assert await run_limited("echo ${BOT_TOKEN:-absent} ${API_HASH:-absent}") == b"absent absent"


@needs_prlimit
async def test_tools_run_under_memory_and_file_size_limits() -> None:
    assert await run_limited("ulimit -v") == str(media.MEMORY_LIMIT_BYTES // 1024).encode()
    assert await run_limited("ulimit -f", max_file_bytes=1024 * 1024) == b"2048"
    # First to be killed if memory runs out, so the bot survives it.
    assert await run_limited("cat /proc/self/oom_score_adj") == b"1000"


@needs_ffmpeg
async def test_a_probe_that_says_too_much_is_refused(
    make_clip: MakeClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = str(make_clip("film.mkv"))
    assert await media.probe(clip) is not None
    monkeypatch.setattr(media, "PROBE_OUTPUT_LIMIT_BYTES", 100)
    assert await media.probe(clip) is None


def test_the_remux_bound_allows_for_audio_converted_to_aac() -> None:
    three_hours = 3 * 3600.0
    copied = media.MediaInfo(frozenset({"matroska"}), "h264", ("aac",), three_hours)
    converted = media.MediaInfo(frozenset({"matroska"}), "h264", ("ac3", "ac3"), three_hours)
    size = 100 * 1024**2

    assert media.remux_bound(size, copied) == size * 11 // 10 + 16 * 1024**2
    growth = media.remux_bound(size, converted) - media.remux_bound(size, copied)
    assert growth == 2 * int(three_hours * media.AAC_BITRATE / 8)


@pytest.fixture
def long_quiet_film(tmp_path: Path) -> Path:
    """30 seconds of tiny video and 32 kbps AC-3: it grows as AAC.

    How much it grows depends on the bitrates, not the length, so a short clip
    proves the same thing as a long film.
    """
    film = tmp_path / "long.mkv"
    command = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    command += ["-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=30"]
    command += ["-f", "lavfi", "-i", "sine=duration=30", "-c:v", "mpeg4"]
    command += ["-c:a", "ac3", "-b:a", "32k", "-shortest", str(film)]
    subprocess.run(command, check=True)
    return film


@needs_ffmpeg
async def test_a_film_that_grows_when_its_audio_is_converted(
    long_quiet_film: Path, tmp_path: Path
) -> None:
    info = await media.probe(str(long_quiet_film))
    assert info is not None
    target = tmp_path / "long.mp4"

    assert await media.remux(str(long_quiet_film), str(target), info)

    grown = target.stat().st_size
    source = long_quiet_film.stat().st_size
    # Far more than the 10% a copy is allowed. The old bound only let this
    # through thanks to a fixed 16 MB of slack, which a long enough film uses up.
    assert grown > source * 2
    assert grown <= media.remux_bound(source, info)
