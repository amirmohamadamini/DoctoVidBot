from __future__ import annotations

import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from doctovid.backup import DATABASE_NAME, Backups, make_backup
from doctovid.store import Store


class FakeClient:
    def __init__(self, unreachable: frozenset[int] = frozenset()) -> None:
        self.unreachable = unreachable
        self.sent: list[tuple[int, list[str]]] = []

    async def send_file(self, chat_id: int, path: str, **kwargs: Any) -> None:
        if chat_id in self.unreachable:
            raise ValueError("never started the bot")
        with zipfile.ZipFile(path) as archive:
            self.sent.append((chat_id, archive.namelist()))


def backups(
    store: Store, client: FakeClient, tmp_path: Path, clock: list[float], **kw: Any
) -> Backups:
    options: dict[str, Any] = {"admins": frozenset({1, 2}), "interval_hours": 12}
    options.update(kw)
    return Backups(
        client,
        store,
        directory=tmp_path / "backups",
        now=lambda: clock[0],
        calendar_now=lambda: datetime(2026, 9, 24, 12, 0),
        **options,
    )


def test_the_backup_is_a_zip_of_a_working_database(store: Store, tmp_path: Path) -> None:
    store.seen(42)
    archive = make_backup(store, tmp_path, datetime(2026, 9, 24, 9, 5))
    assert archive.name == "doctovid-backup-2026-09-24-0905.zip"

    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == [DATABASE_NAME]
        zipped.extract(DATABASE_NAME, tmp_path / "restored")
    restored = sqlite3.connect(tmp_path / "restored" / DATABASE_NAME)
    assert restored.execute("SELECT user_id FROM users").fetchall() == [(42,)]


async def test_sent_to_every_admin_every_twelve_hours(store: Store, tmp_path: Path) -> None:
    clock = [1_000_000.0]
    client = FakeClient()
    scheduler = backups(store, client, tmp_path, clock)

    assert scheduler.due()
    assert await scheduler.send() == 2
    assert client.sent == [(1, [DATABASE_NAME]), (2, [DATABASE_NAME])]
    assert list((tmp_path / "backups").iterdir()) == []

    clock[0] += 11 * 3600
    assert not scheduler.due()
    clock[0] += 3600
    assert scheduler.due()


async def test_the_schedule_survives_a_restart(store: Store, tmp_path: Path) -> None:
    clock = [1_000_000.0]
    await backups(store, FakeClient(), tmp_path, clock).send()
    clock[0] += 3600
    assert not backups(store, FakeClient(), tmp_path, clock).due()


async def test_one_unreachable_admin_does_not_stop_the_others(store: Store, tmp_path: Path) -> None:
    client = FakeClient(unreachable=frozenset({1}))
    assert await backups(store, client, tmp_path, [0.0]).send() == 1


def test_zero_hours_turns_backups_off(store: Store, tmp_path: Path) -> None:
    assert not backups(store, FakeClient(), tmp_path, [0.0], interval_hours=0).due()
    assert not backups(store, FakeClient(), tmp_path, [0.0], admins=frozenset()).due()


async def test_last_delivered_is_tracked_apart_from_last_attempted(
    store: Store, tmp_path: Path
) -> None:
    clock = [1_000_000.0]
    nobody_home = backups(store, FakeClient(unreachable=frozenset({1, 2})), tmp_path, clock)
    assert await nobody_home.send() == 0
    assert nobody_home.describe() == "every 12 h, never delivered"
    assert not nobody_home.due()

    clock[0] += 13 * 3600
    working = backups(store, FakeClient(), tmp_path, clock)
    await working.send()
    clock[0] += 3 * 3600
    assert working.describe() == "every 12 h, last delivered 3 h ago"
