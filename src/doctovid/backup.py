"""Regular backups of the database, sent to the admins as a zip file."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store

log = logging.getLogger(__name__)

CHECK_EVERY_S = 60.0

#: Only the database. The Telegram session is a login secret: anyone holding it
#: can act as the bot, and it doesn't belong in a chat.
DATABASE_NAME = "doctovid.db"


def make_backup(store: Store, directory: Path, when: datetime) -> Path:
    """A zip holding a consistent copy of the database."""
    archive = directory / f"doctovid-backup-{when:%Y-%m-%d-%H%M}.zip"
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        snapshot = Path(scratch) / DATABASE_NAME
        store.snapshot(snapshot)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
            zipped.write(snapshot, DATABASE_NAME)
    return archive


class Backups:
    """Due-based rather than on a timer: the time of the last backup is stored,
    so a restart neither skips one nor sends an extra."""

    def __init__(
        self,
        client: Any,
        store: Store,
        *,
        admins: frozenset[int],
        interval_hours: int,
        directory: Path,
        now: Callable[[], float] = time.time,
        calendar_now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._client = client
        self._store = store
        self._admins = admins
        self._interval_s = interval_hours * 3600
        self._directory = directory
        self._now = now
        self._calendar_now = calendar_now

    @property
    def enabled(self) -> bool:
        return self._interval_s > 0 and bool(self._admins)

    def last_delivered(self) -> float | None:
        """When an admin last actually received one. Separate from the last
        attempt, which is what the schedule counts from."""
        value = self._store.setting("last_backup_delivered")
        return None if value is None else float(value)

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        when = self.last_delivered()
        last = "never delivered" if when is None else f"last delivered {_ago(self._now() - when)}"
        return f"every {self._interval_s // 3600} h, {last}"

    def due(self) -> bool:
        last = float(self._store.setting("last_backup") or 0)
        return self.enabled and self._now() - last >= self._interval_s

    async def run_forever(self) -> None:
        while True:
            if self.due():
                try:
                    await self.send()
                except Exception:
                    log.exception("backup failed")
            await asyncio.sleep(CHECK_EVERY_S)

    async def send(self) -> int:
        """Send a backup to every admin. Returns how many received it."""
        # Recorded first: a backup that fails for every admin must not be
        # retried every minute until it succeeds.
        self._store.set_setting("last_backup", str(self._now()))
        self._directory.mkdir(parents=True, exist_ok=True)
        archive = make_backup(self._store, self._directory, self._calendar_now())
        users, active = self._store.user_counts()
        caption = f"Backup of the DoctoVid database. {users} users, {active} active this month."
        delivered = 0
        try:
            for admin in sorted(self._admins):
                try:
                    await self._client.send_file(
                        admin, str(archive), caption=caption, force_document=True
                    )
                    delivered += 1
                except Exception:
                    # Usually an admin who has never started the bot, which
                    # Telegram requires before a bot may message them.
                    log.warning("could not send the backup to admin %s", admin, exc_info=True)
        finally:
            archive.unlink(missing_ok=True)
        if delivered:
            self._store.set_setting("last_backup_delivered", str(self._now()))
        else:
            log.warning("the backup reached none of the %s admins", len(self._admins))
        log.info("backup sent to %s of %s admins", delivered, len(self._admins))
        return delivered


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"
