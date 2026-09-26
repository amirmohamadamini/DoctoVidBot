"""Sending one message to everyone who has used the bot."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telethon.errors import (
    FloodWaitError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    RPCError,
    UserIsBlockedError,
)

from .store import Store

log = logging.getLogger(__name__)

#: Seconds between messages. Telegram allows bots about 30 messages a second
#: to different chats; staying well under it avoids long FloodWaits.
PACE_S = 0.05

#: A FloodWait longer than this ends the broadcast rather than stalling it.
MAX_WAIT_S = 600

#: How often the admin's progress message is updated.
REPORT_EVERY_S = 10.0

#: Errors that mean this person can never be reached. They are skipped from
#: now on, until they use the bot again.
_UNREACHABLE = (UserIsBlockedError, InputUserDeactivatedError, PeerIdInvalidError)


@dataclass(slots=True)
class Tally:
    total: int
    sent: int = 0
    unreachable: int = 0
    failed: int = 0
    stopped: bool = False

    @property
    def done(self) -> int:
        return self.sent + self.unreachable + self.failed

    def describe(self) -> str:
        text = (
            f"{self.done} of {self.total}: {self.sent} sent, "
            f"{self.unreachable} unreachable, {self.failed} failed"
        )
        if self.stopped:
            text += ". Stopped early: Telegram asked the bot to wait too long"
        return text


Report = Callable[[Tally], Awaitable[None]]


class Broadcaster:
    """One broadcast at a time. The message is copied, so it arrives without a
    "forwarded from" header and without revealing the admin who sent it."""

    def __init__(
        self,
        client: Any,
        store: Store,
        *,
        pace_s: float = PACE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._store = store
        self._pace_s = pace_s
        self._sleep = sleep
        self._now = now
        self.running = False

    async def send(self, message: Any, report: Report) -> Tally:
        if self.running:
            raise RuntimeError("a broadcast is already running")
        self.running = True
        try:
            return await self._send(message, report)
        finally:
            self.running = False

    async def _send(self, message: Any, report: Report) -> Tally:
        users = self._store.reachable_users()
        tally = Tally(total=len(users))
        last_report = self._now()
        for user_id in users:
            if not await self._deliver(user_id, message, tally):
                tally.stopped = True
                break
            if self._now() - last_report >= REPORT_EVERY_S:
                last_report = self._now()
                await report(tally)
            await self._sleep(self._pace_s)
        return tally

    async def _deliver(self, user_id: int, message: Any, tally: Tally) -> bool:
        """Count one delivery. False means stop the whole broadcast."""
        for _ in range(3):
            try:
                await self._client.send_message(user_id, message)
            except FloodWaitError as exc:
                if exc.seconds > MAX_WAIT_S:
                    log.warning("broadcast stopped by a %s s FloodWait", exc.seconds)
                    return False
                await self._sleep(exc.seconds + 1)
                continue
            except _UNREACHABLE:
                self._store.mark_blocked(user_id)
                tally.unreachable += 1
                return True
            except RPCError as exc:
                log.info("broadcast to %s failed: %s", user_id, exc)
                tally.failed += 1
                return True
            tally.sent += 1
            return True
        tally.failed += 1
        return True
