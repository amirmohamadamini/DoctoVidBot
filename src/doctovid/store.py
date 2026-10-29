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
    channel    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (channel, key)
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0,
    language   TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    files   INTEGER NOT NULL,
    bytes   INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS senders (
    document_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    sent_at     INTEGER NOT NULL,
    PRIMARY KEY (document_id, user_id)
);
CREATE TABLE IF NOT EXISTS bans (
    user_id     INTEGER PRIMARY KEY,
    banned_at   INTEGER NOT NULL,
    document_id INTEGER
);
CREATE TABLE IF NOT EXISTS banned_files (
    document_id INTEGER PRIMARY KEY,
    banned_at   INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._upgrade()

    def _upgrade(self) -> None:
        """Columns added since the first release, for a database made before."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(users)")}
        if "language" not in columns:
            self._db.execute("ALTER TABLE users ADD COLUMN language TEXT")
        # A message id means nothing without its channel, and entries from
        # before the channel was stored can't say which one they meant. They
        # are dropped: the files are made again the next time they're asked for.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(cache)")}
        if "channel" not in columns:
            self._db.execute("DROP TABLE cache")
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

    # Every entry belongs to a channel: message ids are only unique within one,
    # so an id read against another channel would be some unrelated message.

    def cached(self, channel: int, key: str) -> int | None:
        row = self._db.execute(
            "SELECT message_id FROM cache WHERE channel = ? AND key = ?", (channel, key)
        ).fetchone()
        return None if row is None else int(row[0])

    def remember(self, channel: int, key: str, message_id: int) -> None:
        self._db.execute(
            "INSERT INTO cache (channel, key, message_id, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (channel, key) DO UPDATE SET message_id = excluded.message_id, "
            "created_at = excluded.created_at",
            (channel, key, message_id, int(time.time())),
        )

    def forget(self, channel: int, key: str) -> None:
        self._db.execute("DELETE FROM cache WHERE channel = ? AND key = ?", (channel, key))

    def cache_key_at(self, channel: int, message_id: int) -> str | None:
        """The entry a message in the cache channel was stored under."""
        row = self._db.execute(
            "SELECT key FROM cache WHERE channel = ? AND message_id = ?", (channel, message_id)
        ).fetchone()
        return None if row is None else str(row[0])

    def cache_entries(self, channel: int) -> list[tuple[str, int]]:
        """(key, message id) of everything cached in `channel`."""
        rows = self._db.execute(
            "SELECT key, message_id FROM cache WHERE channel = ? ORDER BY message_id", (channel,)
        )
        return [(str(row[0]), int(row[1])) for row in rows]

    def cache_size(self, channel: int | None) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM cache WHERE channel = ?", (channel,)
        ).fetchone()
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

    def language(self, user_id: int) -> str | None:
        """The language someone chose, or None if they haven't yet."""
        row = self._db.execute(
            "SELECT language FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def set_language(self, user_id: int, code: str) -> None:
        now = int(time.time())
        self._db.execute(
            "INSERT INTO users (user_id, first_seen, last_seen, language) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET language = excluded.language",
            (user_id, now, now, code),
        )

    def reachable_users(self) -> list[int]:
        rows = self._db.execute(
            "SELECT user_id FROM users WHERE blocked = 0 "
            "AND user_id NOT IN (SELECT user_id FROM bans) ORDER BY user_id"
        )
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

    # ------------------------------------------------------------ bans

    # A Telegram file keeps its document id however many times it is
    # forwarded, and the cache index is keyed by the id of the file the user
    # sent. So a file in the cache channel leads back to everyone who sent it.

    def sent(self, document_id: int, user_id: int) -> None:
        self._db.execute(
            "INSERT INTO senders (document_id, user_id, sent_at) VALUES (?, ?, ?) "
            "ON CONFLICT (document_id, user_id) DO UPDATE SET sent_at = excluded.sent_at",
            (document_id, user_id, int(time.time())),
        )

    def senders(self, document_id: int) -> list[int]:
        rows = self._db.execute(
            "SELECT user_id FROM senders WHERE document_id = ? ORDER BY user_id", (document_id,)
        )
        return [int(row[0]) for row in rows]

    def cached_from(self, channel: int, users: tuple[int, ...]) -> list[tuple[str, int]]:
        """(key, message id) of every file in `channel` made from one of these
        people sent. A file several people sent is in it once, for all of them."""
        if not users:
            return []
        marks = ", ".join("?" * len(users))
        rows = self._db.execute(
            "SELECT DISTINCT c.key, c.message_id FROM cache c JOIN senders s "
            "ON substr(c.key, 1, instr(c.key, ':') - 1) = CAST(s.document_id AS TEXT) "
            f"WHERE c.channel = ? AND s.user_id IN ({marks}) ORDER BY c.message_id",
            (channel, *users),
        )
        return [(str(row[0]), int(row[1])) for row in rows]

    def is_banned(self, user_id: int) -> bool:
        row = self._db.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

    def ban(self, user_id: int, document_id: int | None = None) -> None:
        """`document_id` is the file they were banned for, if any. A ban
        already in place keeps its reason."""
        self._db.execute(
            "INSERT INTO bans (user_id, banned_at, document_id) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, int(time.time()), document_id),
        )

    def unban(self, user_id: int) -> bool:
        """False if they weren't banned."""
        return self._db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,)).rowcount > 0

    def is_file_banned(self, document_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM banned_files WHERE document_id = ?", (document_id,)
        ).fetchone()
        return row is not None

    def ban_file(self, document_id: int) -> None:
        self._db.execute(
            "INSERT INTO banned_files (document_id, banned_at) VALUES (?, ?) "
            "ON CONFLICT (document_id) DO NOTHING",
            (document_id, int(time.time())),
        )

    def unban_file(self, document_id: int) -> tuple[bool, int]:
        """Lifts the file's ban and every ban that was for sending it.
        (whether the file was banned, how many people were unbanned)."""
        was_banned = (
            self._db.execute(
                "DELETE FROM banned_files WHERE document_id = ?", (document_id,)
            ).rowcount
            > 0
        )
        people = self._db.execute("DELETE FROM bans WHERE document_id = ?", (document_id,)).rowcount
        return was_banned, people

    def ban_counts(self) -> tuple[int, int]:
        """(people banned, files banned)."""
        row = self._db.execute(
            "SELECT (SELECT COUNT(*) FROM bans), (SELECT COUNT(*) FROM banned_files)"
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
