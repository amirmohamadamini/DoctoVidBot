from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from doctovid.quota import GB, Calendar, Limits, Usage, refusal, summary

LIMITS = Limits(files_per_day=3, files_per_month=10, gb_per_day=2, gb_per_month=5)


def at(moment: datetime, zone: str = "UTC") -> Calendar:
    return Calendar(zone, now=lambda tz: moment.replace(tzinfo=tz))


CALENDAR = at(datetime(2026, 9, 24, 21, 30))


def check(
    today: Usage, month: Usage | None = None, size: int = 1, limits: Limits = LIMITS
) -> str | None:
    return refusal(limits, today=today, this_month=month or today, size=size, calendar=CALENDAR)


def test_within_every_limit() -> None:
    assert check(Usage(2, GB)) is None


def test_the_daily_file_count() -> None:
    reason = check(Usage(3, 0))
    assert reason == "You've reached today's limit of 3 files. It resets in 2 h 30 min."


def test_the_monthly_file_count() -> None:
    reason = check(Usage(0, 0), Usage(10, 0))
    assert reason == "You've reached this month's limit of 10 files. It resets on 1 October."


def test_the_daily_volume_counts_the_new_file() -> None:
    reason = check(Usage(1, GB), size=GB + 1)
    assert reason is not None
    assert reason.startswith("That file would take you past today's limit of 2.0 GB")
    assert "you've used 1.0 GB" in reason


def test_the_monthly_volume() -> None:
    reason = check(Usage(0, 0), Usage(4, 5 * GB), size=1)
    assert reason is not None
    assert "this month's limit of 5.0 GB" in reason


def test_zero_means_no_limit() -> None:
    unlimited = Limits(0, 0, 0, 0)
    assert check(Usage(10**6, 10**15), size=10**12, limits=unlimited) is None


def test_the_day_follows_the_configured_time_zone() -> None:
    # 22:00 UTC on the 24th is already the 25th in Tehran (+03:30).
    moment = datetime(2026, 9, 24, 22, 0, tzinfo=ZoneInfo("UTC"))
    tehran = Calendar("Asia/Tehran", now=moment.astimezone)
    assert tehran.today() == date(2026, 9, 25)
    assert tehran.until_tomorrow() == timedelta(hours=22, minutes=30)


@pytest.mark.parametrize(
    ("today", "first_of_next"),
    [(datetime(2026, 9, 24), date(2026, 10, 1)), (datetime(2026, 12, 31), date(2027, 1, 1))],
)
def test_next_month(today: datetime, first_of_next: date) -> None:
    assert at(today).next_month() == first_of_next


def test_summary() -> None:
    text = summary(LIMITS, Usage(1, GB), Usage(4, 3 * GB))
    assert (
        text == "Today: 1 of 3 files, 1.0 GB of 2.0 GB\nThis month: 4 of 10 files, 3.0 GB of 5.0 GB"
    )
    assert summary(Limits(0, 0, 0, 0), Usage(1, 0), Usage(1, 0)).startswith("Today: 1 files, 0 B")
