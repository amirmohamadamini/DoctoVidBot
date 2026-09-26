"""Daily and monthly limits on how much each person can send through the bot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import progress

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


def refusal(
    limits: Limits,
    *,
    today: Usage,
    this_month: Usage,
    size: int,
    calendar: Calendar,
) -> str | None:
    """Why one more file of `size` bytes is refused, or None if it is allowed.

    `today` and `this_month` should include files still queued or running, or
    someone could queue far past a limit before any of it was counted.
    """
    day_reset = f"It resets in {_span(calendar.until_tomorrow())}."
    first = calendar.next_month()
    month_reset = f"It resets on {first.day} {first:%B}."
    checks = [
        (limits.files_per_day, today.files + 1, "today's limit", "files", day_reset),
        (limits.files_per_month, this_month.files + 1, "this month's limit", "files", month_reset),
        (limits.gb_per_day * GB, today.bytes + size, "today's limit", "bytes", day_reset),
        (
            limits.gb_per_month * GB,
            this_month.bytes + size,
            "this month's limit",
            "bytes",
            month_reset,
        ),
    ]
    for limit, would_be, period, unit, reset in checks:
        if limit and would_be > limit:
            if unit == "files":
                return f"You've reached {period} of {limit} files. {reset}"
            used = would_be - size
            return (
                f"That file would take you past {period} of {progress.size(limit)} "
                f"(you've used {progress.size(used)}). {reset}"
            )
    return None


def summary(limits: Limits, today: Usage, this_month: Usage) -> str:
    def line(label: str, usage: Usage, files: int, gb: int) -> str:
        count = f"{usage.files} of {files} files" if files else f"{usage.files} files"
        size = progress.size(usage.bytes)
        volume = f"{size} of {progress.size(gb * GB)}" if gb else size
        return f"{label}: {count}, {volume}"

    return "\n".join(
        [
            line("Today", today, limits.files_per_day, limits.gb_per_day),
            line("This month", this_month, limits.files_per_month, limits.gb_per_month),
        ]
    )


def _span(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"
