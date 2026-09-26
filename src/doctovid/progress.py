"""Progress text, and the throttle that keeps edits under Telegram's limits."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from html import escape

BAR_WIDTH = 12

#: Called with (done, total): bytes for a transfer, seconds for a remux.
ProgressCallback = Callable[[int, int], Awaitable[None]]


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def bar(done: float, total: float) -> str:
    fraction = min(1.0, done / total) if total > 0 else 0.0
    filled = round(fraction * BAR_WIDTH)
    return f"{'▰' * filled}{'▱' * (BAR_WIDTH - filled)} {fraction:.0%}"


def transfer(verb: str, name: str, done: int, total: int, speed: float) -> str:
    """`verb` is "⬇️ Downloading" or "⬆️ Uploading"; the numbers are bytes."""
    lines = [f"{verb} <b>{escape(name)}</b>", bar(done, total), f"{size(done)} of {size(total)}"]
    if speed > 0:
        lines[-1] += f" · {size(speed)}/s"
    return "\n".join(lines)


def remux(name: str, done_s: int, total_s: int) -> str:
    return "\n".join(
        [
            f"🎬 Repacking <b>{escape(name)}</b> for streaming",
            bar(done_s, total_s),
            f"{clock(done_s)} of {clock(total_s)}" if total_s else clock(done_s),
        ]
    )


class Throttle:
    """Lets an update through at most once per `interval`, and only if it changed.

    Telethon reports every part of a transfer, thousands of times for a large
    file. Editing a message that often earns a FloodWait for the whole bot.
    """

    def __init__(self, interval: float, now: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._now = now
        self._last_at: float | None = None
        self._last_text = ""

    def due(self) -> bool:
        return self._last_at is None or self._now() - self._last_at >= self._interval

    def admit(self, text: str) -> bool:
        if not self.due() or text == self._last_text:
            return False
        self._last_at = self._now()
        self._last_text = text
        return True


class Speed:
    """Bytes per second between admitted updates, not between callbacks.

    A rate over the few milliseconds between two parts is noise, and the
    number people read to judge whether something is stuck should not flicker.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._mark: tuple[int, float] | None = None
        self.value = 0.0

    def update(self, done: int) -> None:
        at = self._now()
        if self._mark is not None:
            elapsed = at - self._mark[1]
            if elapsed > 0:
                self.value = max(0.0, (done - self._mark[0]) / elapsed)
        self._mark = (done, at)
