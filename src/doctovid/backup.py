"""Regular backups of the database, sent to the admins as a zip file."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .i18n import Html, Locale
from .store import Store

log = logging.getLogger(__name__)

CHECK_EVERY_S = 60.0

#: Only the database. The Telegram session is a login secret: anyone holding it
#: can act as the bot, and it doesn't belong in a chat.
DATABASE_NAME = "doctovid.db"


async def make_backup(store: Store, directory: Path, when: datetime) -> Path:
    """A zip holding a consistent copy of the database, in a folder of its own
    under `directory`, so two backups never share a file. The caller removes
    the folder."""
    folder = Path(tempfile.mkdtemp(dir=directory, prefix="backup-"))
    try:
        snapshot = folder / DATABASE_NAME
        # The copy is taken on the bot's own connection, which SQLite ties to
        # this thread; it's quick. Compressing it isn't, so that runs in a
        # thread of its own, and the bot keeps answering meanwhile.
        store.snapshot(snapshot)
        archive = folder / f"doctovid-backup-{when:%Y-%m-%d-%H%M}.zip"
        await asyncio.to_thread(_compress, snapshot, archive)
        snapshot.unlink()
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return archive


def _compress(snapshot: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        zipped.write(snapshot, DATABASE_NAME)


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
        locale_for: Callable[[int], Locale],
    ) -> None:
        self._client = client
        self._store = store
        self._admins = admins
        self._interval_s = interval_hours * 3600
        self._directory = directory
        self._now = now
        self._calendar_now = calendar_now
        self._locale_for = locale_for
        #: One backup at a time: the schedule and a tap on "Backup now" can
        #: coincide, and so can two taps.
        self._sending = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._interval_s > 0 and bool(self._admins)

    def last_delivered(self) -> float | None:
        """When an admin last actually received one. Separate from the last
        attempt, which is what the schedule counts from."""
        value = self._store.setting("last_backup_delivered")
        return None if value is None else float(value)

    def describe(self, t: Locale) -> Html:
        if not self.enabled:
            return t.phrase("backup.off")
        when = self.last_delivered()
        last = (
            t.phrase("backup.never")
            if when is None
            else t.phrase("backup.last", ago=_ago(self._now() - when, t))
        )
        return t.phrase("backup.schedule", hours=self._interval_s // 3600, last=last)

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
        async with self._sending:
            return await self._send()

    async def _send(self) -> int:
        # Recorded first: a backup that fails for every admin must not be
        # retried every minute until it succeeds.
        self._store.set_setting("last_backup", str(self._now()))
        self._directory.mkdir(parents=True, exist_ok=True)
        archive = await make_backup(self._store, self._directory, self._calendar_now())
        users, active = self._store.user_counts()
        delivered = 0
        try:
            for admin in sorted(self._admins):
                try:
                    caption = self._locale_for(admin).text(
                        "backup.caption", users=users, active=active
                    )
                    await self._client.send_file(
                        admin, str(archive), caption=caption, force_document=True
                    )
                    delivered += 1
                except Exception:
                    # Usually an admin who has never started the bot, which
                    # Telegram requires before a bot may message them.
                    log.warning("could not send the backup to admin %s", admin, exc_info=True)
        finally:
            shutil.rmtree(archive.parent, ignore_errors=True)
        if delivered:
            self._store.set_setting("last_backup_delivered", str(self._now()))
        else:
            log.warning("the backup reached none of the %s admins", len(self._admins))
        log.info("backup sent to %s of %s admins", delivered, len(self._admins))
        return delivered


def _ago(seconds: float, t: Locale) -> Html:
    minutes = int(seconds // 60)
    if minutes < 1:
        return t.phrase("backup.just_now")
    if minutes < 60:
        return t.phrase("backup.minutes_ago", count=minutes)
    hours = minutes // 60
    if hours < 48:
        return t.phrase("backup.hours_ago", count=hours)
    return t.phrase("backup.days_ago", count=hours // 24)
