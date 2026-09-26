"""Settings, read once from the environment at startup."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .quota import Limits

#: Telegram's ceiling for a file a bot uploads: 4000 parts of 512 KiB.
TELEGRAM_MAX_FILE_MB = 2000

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Every problem with the settings at once, so one restart fixes them all."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: frozenset[int]
    cache_channel_id: int | None
    scratch_dir: Path
    media_socket: Path | None
    join_channel: int | str | None
    join_link: str | None
    backup_interval_hours: int
    data_dir: Path
    max_file_mb: int
    workers: int
    max_queue: int
    jobs_per_user: int
    menu_timeout_s: int
    progress_interval_s: float
    user_limits: Limits
    admin_limits: Limits
    timezone: str
    log_level: str

    def limits_for(self, user_id: int) -> Limits:
        return self.admin_limits if user_id in self.admin_ids else self.user_limits

    @property
    def session_path(self) -> Path:
        return self.data_dir / "bot.session"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "doctovid.db"

    @property
    def work_dir(self) -> Path:
        """Where jobs download and convert files. In production this is the
        scratch volume shared with the media worker, never `/data`."""
        return self.scratch_dir

    @property
    def thumbnail_dir(self) -> Path:
        """Finished custom thumbnails, waiting for their file to be sent.
        Private to the bot: only a copy made after the worker is done lands here."""
        return self.data_dir / "thumbs"

    @property
    def backup_dir(self) -> Path:
        """Backups are built here, next to the database and away from the
        scratch volume, which the media worker can read."""
        return self.data_dir / "backup-tmp"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


class _Reader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.problems: list[str] = []

    def text(self, key: str, default: str | None = None) -> str:
        value = self._env.get(key, "").strip()
        if value:
            return value
        if default is None:
            self.problems.append(f"{key} is required")
            return ""
        return default

    def integer(
        self,
        key: str,
        default: int | None = None,
        *,
        low: int | None = None,
        high: int | None = None,
    ) -> int:
        raw = self.text(key, None if default is None else str(default))
        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError:
            self.problems.append(f"{key} must be a whole number, got {raw!r}")
            return 0
        if (low is not None and value < low) or (high is not None and value > high):
            span = f"between {low} and {high}" if high is not None else f"at least {low}"
            self.problems.append(f"{key} must be {span}, got {value}")
        return value

    def ids(self, key: str) -> frozenset[int]:
        found: set[int] = set()
        for part in self._env.get(key, "").replace(" ", "").split(","):
            if not part:
                continue
            try:
                found.add(int(part))
            except ValueError:
                self.problems.append(f"{key} must be comma-separated numeric ids, got {part!r}")
        return frozenset(found)

    def limits(
        self, prefix: str, files_day: int, files_month: int, gb_day: int, gb_month: int
    ) -> Limits:
        return Limits(
            files_per_day=self.integer(f"{prefix}_DAILY_FILES", files_day, low=0),
            files_per_month=self.integer(f"{prefix}_MONTHLY_FILES", files_month, low=0),
            gb_per_day=self.integer(f"{prefix}_DAILY_GB", gb_day, low=0),
            gb_per_month=self.integer(f"{prefix}_MONTHLY_GB", gb_month, low=0),
        )

    def choice(self, key: str, default: str, allowed: tuple[str, ...]) -> str:
        value = self.text(key, default).upper()
        if value not in allowed:
            self.problems.append(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
        return value

    def zone(self, key: str, default: str) -> str:
        name = self.text(key, default)
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            self.problems.append(
                f"{key} must be a time zone such as UTC or Asia/Tehran, got {name!r}"
            )
        return name

    def join(self, key: str, link_key: str) -> tuple[int | str | None, str | None]:
        """A public channel by @username, or a private one by id plus its invite link."""
        raw = self._env.get(key, "").strip()
        link = self._env.get(link_key, "").strip() or None
        if not raw:
            return None, None
        if link is not None and not link.startswith("https://t.me/"):
            self.problems.append(f"{link_key} must be a https://t.me/ link, got {link!r}")
        if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", raw):
            username = raw.removeprefix("@")
            return username, link or f"https://t.me/{username}"
        if raw.startswith("-100") and raw[1:].isdigit():
            if link is None:
                self.problems.append(f"{link_key} is required when {key} is a numeric id")
            return int(raw), link
        self.problems.append(f"{key} must be @username or an id starting with -100, got {raw!r}")
        return None, None

    def channel(self, key: str) -> int | None:
        raw = self._env.get(key, "").strip()
        if not raw:
            return None
        if not raw.startswith("-100") or not raw[1:].isdigit():
            self.problems.append(f"{key} must be a channel id starting with -100, got {raw!r}")
            return None
        return int(raw)


def load(env: Mapping[str, str] | None = None) -> Config:
    read = _Reader(os.environ if env is None else env)
    join_channel, join_link = read.join("JOIN_CHANNEL", "JOIN_CHANNEL_LINK")
    data_dir = Path(read.text("DATA_DIR", "data"))
    socket = read.text("MEDIA_SOCKET", "")
    config = Config(
        api_id=read.integer("API_ID"),
        api_hash=read.text("API_HASH"),
        bot_token=read.text("BOT_TOKEN"),
        admin_ids=read.ids("ADMIN_IDS"),
        cache_channel_id=read.channel("CACHE_CHANNEL_ID"),
        scratch_dir=Path(read.text("SCRATCH_DIR", str(data_dir / "work"))),
        media_socket=Path(socket) if socket else None,
        join_channel=join_channel,
        join_link=join_link,
        backup_interval_hours=read.integer("BACKUP_INTERVAL_HOURS", 12, low=0),
        data_dir=data_dir,
        max_file_mb=read.integer(
            "MAX_FILE_MB", TELEGRAM_MAX_FILE_MB, low=1, high=TELEGRAM_MAX_FILE_MB
        ),
        workers=read.integer("WORKERS", 2, low=1, high=16),
        max_queue=read.integer("MAX_QUEUE", 50, low=1),
        jobs_per_user=read.integer("JOBS_PER_USER", 3, low=1),
        menu_timeout_s=60 * read.integer("MENU_TIMEOUT_MINUTES", 15, low=1),
        progress_interval_s=float(read.integer("PROGRESS_INTERVAL_SECONDS", 5, low=2)),
        user_limits=read.limits("USER", 20, 300, 10, 100),
        admin_limits=read.limits("ADMIN", 100, 2000, 50, 500),
        timezone=read.zone("TIMEZONE", "UTC"),
        log_level=read.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
    )
    if read.problems:
        raise ConfigError(read.problems)
    return config
