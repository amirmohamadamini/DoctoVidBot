from __future__ import annotations

import asyncio
from typing import Any

import pytest
from telethon.errors import FileReferenceExpiredError, FloodWaitError, RPCError, UserIsBlockedError

from doctovid.broadcast import MAX_WAIT_S, Broadcaster, Refetch, Tally
from doctovid.store import Store

from .conftest import EN


class FakeClient:
    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.delivered: list[int] = []
        self.forwarded: list[int] = []
        #: People whose message never gets an answer.
        self.hangs: set[int] = set()
        #: Media whose file reference has expired.
        self.expired: set[str] = set()

    async def send_message(self, user_id: int, message: Any) -> None:
        pending = self.failures.get(user_id)
        if pending:
            raise pending.pop(0)
        if user_id in self.hangs:
            await asyncio.Event().wait()
        if message in self.expired:
            raise FileReferenceExpiredError(request=None)
        self.delivered.append(user_id)

    async def forward_messages(self, user_id: int, message: Any) -> None:
        await self.send_message(user_id, message)
        self.forwarded.append(user_id)


async def no_report(tally: Tally) -> None:
    pass


async def run(
    client: FakeClient,
    store: Store,
    *,
    forward: bool = False,
    refetch: Refetch | None = None,
    message: str = "hello",
) -> tuple[Tally, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    broadcaster = Broadcaster(client, store, sleep=sleep, send_timeout_s=0.05)
    assert broadcaster.claim()
    tally = await broadcaster.send(message, no_report, forward=forward, refetch=refetch)
    assert not broadcaster.running
    return tally, slept


def users(store: Store, *ids: int) -> None:
    for user_id in ids:
        store.seen(user_id)


async def test_everyone_gets_it(store: Store) -> None:
    users(store, 1, 2, 3)
    client = FakeClient()
    tally, _ = await run(client, store)
    assert client.delivered == [1, 2, 3]
    assert (tally.sent, tally.total) == (3, 3)


async def test_a_forward_is_forwarded_and_handled_the_same(store: Store) -> None:
    users(store, 1, 2, 3)
    client = FakeClient({2: [UserIsBlockedError(request=None)]})
    tally, _ = await run(client, store, forward=True)
    assert client.forwarded == [1, 3]
    assert (tally.sent, tally.unreachable) == (2, 1)


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
    assert "Stopped early" in tally.describe(EN)


async def test_other_errors_are_counted_and_skipped(store: Store) -> None:
    users(store, 1, 2)
    tally, _ = await run(FakeClient({1: [RPCError(request=None, message="X")]}), store)
    assert (tally.sent, tally.failed) == (1, 1)
    assert store.reachable_users() == [1, 2]


@pytest.mark.parametrize("forward", [False, True])
async def test_someone_telethon_cannot_resolve_is_skipped(store: Store, forward: bool) -> None:
    users(store, 1, 2)
    client = FakeClient({1: [ValueError("Could not find the input entity")]})
    tally, _ = await run(client, store, forward=forward)
    assert client.delivered == [2]
    assert (tally.sent, tally.failed, tally.stopped) == (1, 1, False)
    assert store.reachable_users() == [1, 2]


async def test_a_send_that_never_returns_is_given_up(store: Store) -> None:
    users(store, 1, 2)
    client = FakeClient()
    client.hangs.add(1)
    tally, _ = await run(client, store)
    assert client.delivered == [2]
    assert (tally.sent, tally.failed) == (1, 1)


async def test_a_stuck_progress_update_does_not_hold_the_broadcast(store: Store) -> None:
    users(store, 1, 2)
    clock = iter(range(0, 1000, 20))

    async def stuck(tally: Tally) -> None:
        await asyncio.Event().wait()

    async def no_sleep(seconds: float) -> None:
        pass

    client = FakeClient()
    broadcaster = Broadcaster(
        client, store, sleep=no_sleep, now=lambda: next(clock), report_timeout_s=0.05
    )
    assert broadcaster.claim()
    tally = await broadcaster.send("hello", stuck)
    assert client.delivered == [1, 2]
    assert tally.sent == 2


async def test_an_expired_file_reference_is_fetched_again_once(store: Store) -> None:
    users(store, 1, 2, 3)
    client = FakeClient()
    client.expired.add("stale")
    fetched: list[str] = []

    async def refetch() -> str:
        fetched.append("fresh")
        return "fresh"

    tally, _ = await run(client, store, refetch=refetch, message="stale")
    assert client.delivered == [1, 2, 3]
    assert tally.sent == 3
    # Kept for everyone after, not fetched again for each.
    assert fetched == ["fresh"]


async def test_a_draft_gone_for_good_stops_the_broadcast(store: Store) -> None:
    users(store, 1, 2)
    client = FakeClient()
    client.expired.update({"stale", "still stale"})

    async def deleted() -> None:
        return None

    tally, _ = await run(client, store, refetch=deleted, message="stale")
    assert (client.delivered, tally.stopped) == ([], True)

    async def no_better() -> str:
        return "still stale"

    tally, _ = await run(client, store, refetch=no_better, message="stale")
    assert (client.delivered, tally.stopped) == ([], True)


async def test_the_broadcast_slot_is_claimed_first(store: Store) -> None:
    broadcaster = Broadcaster(FakeClient(), store)
    with pytest.raises(RuntimeError):
        await broadcaster.send("hello", no_report)
    assert broadcaster.claim()
    assert not broadcaster.claim()
