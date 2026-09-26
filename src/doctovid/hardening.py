"""Process-level protection shared by the bot and the media worker."""

from __future__ import annotations

import ctypes
import sys

#: From <linux/prctl.h>.
PR_SET_DUMPABLE = 4


def make_undumpable() -> bool:
    """Stop other processes running as this user from reading this one.

    A process's /proc/<pid>/environ and /proc/<pid>/mem are readable by
    anything else running as the same user. For the bot run without the media
    container, that includes ffmpeg, working on files from strangers; for the
    media worker, it's the ffmpeg processes it starts. A process that isn't
    dumpable has those files owned by root instead. Returns whether it worked;
    it only can on Linux.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0)
    except OSError, AttributeError:
        return False
