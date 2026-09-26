from __future__ import annotations

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
    assert store.cached("5:video") is None
    store.remember("5:video", 10)
    store.remember("5:video", 11)
    assert store.cached("5:video") == 11
    assert store.cache_size() == 1
    store.forget("5:video")
    assert store.cached("5:video") is None


def test_state_survives_a_restart(tmp_path: Path) -> None:
    first = Store(tmp_path / "db")
    first.set_cache_enabled(True)
    first.remember("k", 3)
    first.close()
    second = Store(tmp_path / "db")
    assert second.cache_enabled()
    assert second.cached("k") == 3


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
