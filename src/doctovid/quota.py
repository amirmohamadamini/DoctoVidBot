"""Daily and monthly limits on how much each person can send through the bot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .i18n import Html, Locale

GB = 1024**3


@dataclass(frozen=True, slots=True)
class Limits:
    """Zero means no limit."""

    files_per_day: int
    files_per_month: int
    gb_per_day: int
    gb_per_month: int


@dataclass(frozen=True, slots=True)
class Usage:
    files: int = 0
    bytes: int = 0

    def plus(self, files: int, size: int) -> Usage:
        return Usage(self.files + files, self.bytes + size)


class Calendar:
    """Days and months in the bot's time zone, which is when limits reset."""

    def __init__(self, zone: str, now: Callable[[ZoneInfo], datetime] = datetime.now) -> None:
        self._zone = ZoneInfo(zone)
        self._now = now

    def now(self) -> datetime:
        return self._now(self._zone)

    def today(self) -> date:
        return self.now().date()

    def until_tomorrow(self) -> timedelta:
        now = self.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
        return midnight - now

    def next_month(self) -> date:
        today = self.today()
        return date(today.year + today.month // 12, today.month % 12 + 1, 1)

    def until_next_month(self) -> timedelta:
        now = self.now()
        return datetime.combine(self.next_month(), datetime.min.time(), now.tzinfo) - now


def refusal(
    limits: Limits,
    *,
    today: Usage,
    this_month: Usage,
    size: int,
    calendar: Calendar,
    t: Locale,
) -> Html | None:
    """Why one more file of `size` bytes is refused, or None if it is allowed.

    `today` and `this_month` should include files still queued or running, or
    someone could queue far past a limit before any of it was counted.
    """
    day_wait, month_wait = calendar.until_tomorrow(), calendar.until_next_month()
    checks = [
        (limits.files_per_day, today.files + 1, "limit.day_files", day_wait),
        (limits.files_per_month, this_month.files + 1, "limit.month_files", month_wait),
        (limits.gb_per_day * GB, today.bytes + size, "limit.day_volume", day_wait),
        (limits.gb_per_month * GB, this_month.bytes + size, "limit.month_volume", month_wait),
    ]
    for limit, would_be, key, wait in checks:
        if limit and would_be > limit:
            span = t.span(wait.total_seconds())
            if key.endswith("_files"):
                return t.phrase(key, count=limit, wait=span)
            return t.phrase(key, limit=t.size(limit), used=t.size(would_be - size), wait=span)
    return None


def summary(limits: Limits, today: Usage, this_month: Usage, t: Locale) -> Html:
    def line(period: str, usage: Usage, files: int, gb: int) -> Html:
        count = (
            t.phrase("usage.files_of", used=usage.files, count=files)
            if files
            else t.phrase("usage.files", count=usage.files)
        )
        size = t.size(usage.bytes)
        volume = t.phrase("usage.volume_of", used=size, limit=t.size(gb * GB)) if gb else size
        return t.phrase("usage.line", period=t.word(period), files=count, volume=volume)

    return t.lines(
        line("usage.today", today, limits.files_per_day, limits.gb_per_day),
        line("usage.month", this_month, limits.files_per_month, limits.gb_per_month),
    )
