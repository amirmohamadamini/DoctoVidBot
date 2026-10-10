"""The two containers working together, as deployed. Run by CI, not by pytest.

The unit tests run the bot and the worker as one user. This runs in the real
images instead: the worker as user media with no network, the bot as user
doctovid, sharing only the scratch volume. It makes a video, has the worker
probe it, repack it and take a thumbnail, and reads each result back the way
the bot does, so a mistake in the users, groups or volume permissions fails
here rather than on the first file someone sends.

    docker compose up -d media
    docker compose run --rm -T --no-deps bot python - < tests/container_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from doctovid.media import Plan
from doctovid.runner import THUMBNAIL_READ_LIMIT, job_directory
from doctovid.safefile import claim, read_small
from doctovid.tools import RemoteTools, WorkerUnavailable


async def ignore_progress(done: int, total: int) -> None:
    pass


def make_video(scratch: Path, tools: RemoteTools) -> Path:
    """A video in a job directory, shared with the worker as the bot does.

    Matroska with MP2 audio: it needs repacking, and its audio converting, so
    every step runs.
    """
    job = job_directory(scratch, 1)
    job.mkdir()
    tools.share_dir(job)
    source = job / "source"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=duration=3",
            "-c:v", "mpeg4", "-c:a", "mp2", "-shortest", "-f", "matroska", str(source),
        ],
        check=True,
    )  # fmt: skip
    tools.share_file(source)
    return source


async def check(tools: RemoteTools, source: Path) -> None:
    job = source.parent
    info = await tools.probe(source)
    assert info is not None, "the worker couldn't read the video"
    assert info.plan is Plan.REMUX, info

    target = job / "playable.mp4"
    assert await tools.remux(source, target, info, ignore_progress), "the remux failed"
    with claim(target) as file:
        video = file.read()
    assert video.index(b"moov") < video.index(b"mdat"), "the MP4 isn't streamable"

    thumbnail = job / "thumbnail.jpg"
    assert await tools.frame_thumbnail(target, thumbnail, info.duration_s)
    assert read_small(thumbnail, THUMBNAIL_READ_LIMIT).startswith(b"\xff\xd8\xff")

    # The bot's own files are out of the worker's reach.
    try:
        await tools.probe(Path("/data/doctovid.db"))
    except WorkerUnavailable:
        pass
    else:
        raise AssertionError("the worker accepted a path outside the scratch volume")

    print("the bot and the media worker work together")


if __name__ == "__main__":
    remote = RemoteTools(Path(os.environ["MEDIA_SOCKET"]))
    asyncio.run(check(remote, make_video(Path(os.environ["SCRATCH_DIR"]), remote)))
