"""Everything the bot keeps between restarts, in one SQLite file."""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path

from .quota import Usage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    files   INTEGER NOT NULL,
    bytes   INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ settings

    def setting(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def switch(self, key: str) -> bool:
        """An admin's on/off setting. Everything starts off."""
        return self.setting(key) == "on"

    def set_switch(self, key: str, on: bool) -> None:
        self.set_setting(key, "on" if on else "off")

    def cache_enabled(self) -> bool:
        return self.switch("cache")

    def set_cache_enabled(self, enabled: bool) -> None:
        self.set_switch("cache", enabled)

    # ------------------------------------------------------------ cache

    def cached(self, key: str) -> int | None:
        row = self._db.execute("SELECT message_id FROM cache WHERE key = ?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def remember(self, key: str, message_id: int) -> None:
        self._db.execute(
            "INSERT INTO cache (key, message_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET message_id = excluded.message_id, "
            "created_at = excluded.created_at",
            (key, message_id, int(time.time())),
        )

    def forget(self, key: str) -> None:
        self._db.execute("DELETE FROM cache WHERE key = ?", (key,))

    def cache_size(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM cache").fetchone()
        return int(row[0])

    # ------------------------------------------------------------ usage

    def record(self, user_id: int, day: date, size: int) -> None:
        self._db.execute(
            "INSERT INTO usage (user_id, day, files, bytes) VALUES (?, ?, 1, ?) "
            "ON CONFLICT (user_id, day) DO UPDATE SET "
            "files = files + 1, bytes = bytes + excluded.bytes",
            (user_id, day.isoformat(), size),
        )
        # Only this month counts for anything. Last month is kept so a change
        # of time zone near midnight on the 1st can't lose a day.
        previous_month = date(day.year - (day.month == 1), (day.month - 2) % 12 + 1, 1)
        self._db.execute("DELETE FROM usage WHERE day < ?", (previous_month.isoformat(),))

    def usage(self, user_id: int, day: date) -> tuple[Usage, Usage]:
        """What `user_id` has used on `day`, and in the month `day` falls in."""
        row = self._db.execute(
            "SELECT "
            "COALESCE(SUM(files) FILTER (WHERE day = :day), 0), "
            "COALESCE(SUM(bytes) FILTER (WHERE day = :day), 0), "
            "COALESCE(SUM(files), 0), COALESCE(SUM(bytes), 0) "
            "FROM usage WHERE user_id = :user AND day >= :month AND day <= :day",
            {"user": user_id, "day": day.isoformat(), "month": day.replace(day=1).isoformat()},
        ).fetchone()
        return Usage(int(row[0]), int(row[1])), Usage(int(row[2]), int(row[3]))

    # ------------------------------------------------------------ users

    def seen(self, user_id: int) -> None:
        """Someone used the bot. Using it again after blocking it unblocks them."""
        now = int(time.time())
        self._db.execute(
            "INSERT INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET last_seen = excluded.last_seen, blocked = 0",
            (user_id, now, now),
        )

    def reachable_users(self) -> list[int]:
        rows = self._db.execute("SELECT user_id FROM users WHERE blocked = 0 ORDER BY user_id")
        return [int(row[0]) for row in rows]

    def mark_blocked(self, user_id: int) -> None:
        self._db.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))

    def user_counts(self) -> tuple[int, int]:
        """(everyone, those active in the last 30 days)."""
        month_ago = int(time.time()) - 30 * 24 * 3600
        row = self._db.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE last_seen >= ?) FROM users", (month_ago,)
        ).fetchone()
        return int(row[0]), int(row[1])

    # ------------------------------------------------------------ backup

    def snapshot(self, target: Path) -> None:
        """A consistent copy of the database, taken while the bot keeps running."""
        copy = sqlite3.connect(target)
        try:
            self._db.backup(copy)
        finally:
            copy.close()
