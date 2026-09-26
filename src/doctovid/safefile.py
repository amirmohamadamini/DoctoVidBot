"""Opening files that something less trusted could have tampered with.

With the media container, ffmpeg runs as a different user that can write in
each job's directory. If it were ever compromised, it could replace an output
with a symlink to `/data/bot.session`, and the bot, uploading "the result",
would send its own login to a stranger. Or it could leave a FIFO that hangs
the bot when opened.

So every file the bot reads back from a job directory is opened here: never
through a symlink, never unless it is a plain file, and read through the file
descriptor that was checked, so it can't be swapped in between.
"""

from __future__ import annotations

import os
import stat
from typing import BinaryIO


class UnsafeFile(Exception):
    """The path isn't a plain file this process should be reading."""


def claim(path: str | os.PathLike[str]) -> BinaryIO:
    """Open `path` for reading, if it is a regular file reached without a symlink."""
    try:
        # O_NONBLOCK so that opening a FIFO returns at once instead of
        # waiting for a writer; it's cleared again for a regular file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise UnsafeFile(f"can't open {os.fspath(path)!r}: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeFile(f"{os.fspath(path)!r} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_small(path: str | os.PathLike[str], limit: int) -> bytes:
    """The whole of a small file, such as a thumbnail, checked the same way."""
    with claim(path) as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise UnsafeFile(f"{os.fspath(path)!r} is larger than {limit} bytes")
    return data
