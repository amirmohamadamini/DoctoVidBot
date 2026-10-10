from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from doctovid.quota import Usage
from doctovid.store import Store


def test_cache_is_off_until_turned_on(store: Store) -> None:
    assert not store.cache_enabled()
    store.set_cache_enabled(True)
    assert store.cache_enabled()
    store.set_cache_enabled(False)
    assert not store.cache_enabled()


def test_cache_index(store: Store) -> None:
    assert store.cached(-100, "5:video") is None
    store.remember(-100, "5:video", 10)
    store.remember(-100, "5:video", 11)
    assert store.cached(-100, "5:video") == 11
    assert store.cache_size(-100) == 1
    store.forget(-100, "5:video")
    assert store.cached(-100, "5:video") is None


def test_state_survives_a_restart(tmp_path: Path) -> None:
    first = Store(tmp_path / "db")
    first.set_cache_enabled(True)
    first.remember(-100, "k", 3)
    first.close()
    second = Store(tmp_path / "db")
    assert second.cache_enabled()
    assert second.cached(-100, "k") == 3


def test_usage_by_day_and_month(store: Store) -> None:
    store.record(42, date(2026, 9, 23), 100)
    store.record(42, date(2026, 9, 24), 10)
    store.record(42, date(2026, 9, 24), 20)
    store.record(43, date(2026, 9, 24), 999)
    store.record(42, date(2026, 8, 31), 5000)

    today, month = store.usage(42, date(2026, 9, 24))
    assert today == Usage(2, 30)
    assert month == Usage(3, 130)


def test_old_usage_is_pruned(store: Store) -> None:
    store.record(42, date(2026, 6, 1), 1)
    store.record(42, date(2026, 9, 24), 1)
    assert store.usage(42, date(2026, 6, 1))[0] == Usage()


def test_a_language_is_remembered(store: Store) -> None:
    assert store.language(42) is None
    store.seen(42)
    store.set_language(42, "fa")
    store.seen(42)
    assert store.language(42) == "fa"
    assert store.reachable_users() == [42]


def test_a_database_from_before_languages_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_seen INTEGER NOT NULL, "
        "last_seen INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute("INSERT INTO users VALUES (42, 1, 1, 0)")
    old.commit()
    old.close()

    store = Store(path)
    assert store.language(42) is None
    store.set_language(42, "fa")
    assert store.language(42) == "fa"
    assert store.reachable_users() == [42]


def test_cache_entries_belong_to_their_channel(store: Store) -> None:
    store.remember(-100, "5:video", 27)
    assert store.cached(-200, "5:video") is None
    assert store.cache_size(-200) == 0
    store.remember(-200, "5:video", 3)
    assert (store.cached(-100, "5:video"), store.cached(-200, "5:video")) == (27, 3)


def test_cache_entries_without_a_channel_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE cache (key TEXT PRIMARY KEY, message_id INTEGER NOT NULL, "
        "created_at INTEGER NOT NULL)"
    )
    old.execute("INSERT INTO cache VALUES ('5:video', 27, 0)")
    old.commit()
    old.close()

    store = Store(path)
    assert store.cached(-100, "5:video") is None
    store.remember(-100, "5:video", 28)
    assert store.cached(-100, "5:video") == 28
