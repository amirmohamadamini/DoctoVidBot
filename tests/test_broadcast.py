from __future__ import annotations

from typing import Any

from telethon.errors import FloodWaitError, RPCError, UserIsBlockedError

from doctovid.broadcast import MAX_WAIT_S, Broadcaster, Tally
from doctovid.store import Store


class FakeClient:
    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.delivered: list[int] = []

    async def send_message(self, user_id: int, message: Any) -> None:
        pending = self.failures.get(user_id)
        if pending:
            raise pending.pop(0)
        self.delivered.append(user_id)


async def run(client: FakeClient, store: Store) -> tuple[Tally, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def report(tally: Tally) -> None:
        pass

    broadcaster = Broadcaster(client, store, sleep=sleep)
    return await broadcaster.send("hello", report), slept


def users(store: Store, *ids: int) -> None:
    for user_id in ids:
        store.seen(user_id)


async def test_everyone_gets_it(store: Store) -> None:
    users(store, 1, 2, 3)
    client = FakeClient()
    tally, _ = await run(client, store)
    assert client.delivered == [1, 2, 3]
    assert (tally.sent, tally.total) == (3, 3)


async def test_blocked_users_are_skipped_next_time(store: Store) -> None:
    users(store, 1, 2)
    tally, _ = await run(FakeClient({2: [UserIsBlockedError(request=None)]}), store)
    assert (tally.sent, tally.unreachable) == (1, 1)
    assert store.reachable_users() == [1]

    store.seen(2)
    assert store.reachable_users() == [1, 2]


async def test_a_floodwait_is_waited_out(store: Store) -> None:
    users(store, 1)
    client = FakeClient({1: [FloodWaitError(request=None, capture=7)]})
    tally, slept = await run(client, store)
    assert client.delivered == [1]
    assert 8 in slept
    assert tally.sent == 1


async def test_a_very_long_floodwait_stops_the_broadcast(store: Store) -> None:
    users(store, 1, 2)
    client = FakeClient({1: [FloodWaitError(request=None, capture=MAX_WAIT_S + 1)]})
    tally, _ = await run(client, store)
    assert client.delivered == []
    assert tally.stopped
    assert "Stopped early" in tally.describe()


async def test_other_errors_are_counted_and_skipped(store: Store) -> None:
    users(store, 1, 2)
    tally, _ = await run(FakeClient({1: [RPCError(request=None, message="X")]}), store)
    assert (tally.sent, tally.failed) == (1, 1)
    assert store.reachable_users() == [1, 2]
