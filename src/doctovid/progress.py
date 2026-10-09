"""Progress text, and the throttle that keeps edits under Telegram's limits."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from .i18n import Html, Locale

BAR_WIDTH = 12

#: Called with (done, total): bytes for a transfer, seconds for a remux.
ProgressCallback = Callable[[int, int], Awaitable[None]]


def bar(done: float, total: float, t: Locale) -> Html:
    fraction = min(1.0, done / total) if total > 0 else 0.0
    filled = round(fraction * BAR_WIDTH)
    return Html(f"{'▰' * filled}{'▱' * (BAR_WIDTH - filled)} {t.percent(fraction)}")


def transfer(title: str, name: str, done: int, total: int, *, speed: float, t: Locale) -> Html:
    """`title` is `progress.downloading` or `progress.uploading`; the numbers are bytes."""
    amount = t.phrase("progress.amount", done=t.size(done), total=t.size(total))
    if speed > 0:
        amount = t.phrase("progress.with_speed", amount=amount, speed=t.speed(speed))
    return t.lines(t.phrase(title, name=name), bar(done, total, t), amount)


def remux(name: str, done_s: int, total_s: int, t: Locale) -> Html:
    amount = (
        t.phrase("progress.amount", done=t.clock(done_s), total=t.clock(total_s))
        if total_s
        else t.clock(done_s)
    )
    return t.lines(t.phrase("progress.repacking", name=name), bar(done_s, total_s, t), amount)


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
