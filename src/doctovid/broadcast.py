"""Sending one message to everyone who has used the bot."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telethon.errors import (
    FileReferenceExpiredError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    RPCError,
    UserIsBlockedError,
)

from .i18n import Html, Locale
from .store import Store

log = logging.getLogger(__name__)

#: Seconds between messages. Telegram allows bots about 30 messages a second
#: to different chats; staying well under it avoids long FloodWaits.
PACE_S = 0.05

#: A FloodWait longer than this ends the broadcast rather than stalling it.
MAX_WAIT_S = 600

#: How often the admin's progress message is updated.
REPORT_EVERY_S = 10.0

#: How long one message, or one progress update, may take. A request Telegram
#: never answers would otherwise hold the broadcast, and everyone after it.
SEND_TIMEOUT_S = 60.0
REPORT_TIMEOUT_S = 15.0

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

    def describe(self, t: Locale) -> Html:
        text = t.phrase(
            "broadcast.tally",
            done=self.done,
            total=self.total,
            sent=self.sent,
            unreachable=self.unreachable,
            failed=self.failed,
        )
        return t.phrase("broadcast.stopped", tally=text) if self.stopped else text


Report = Callable[[Tally], Awaitable[None]]
Refetch = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class _Run:
    message: Any
    forward: bool
    refetch: Refetch | None
    tally: Tally


class Broadcaster:
    """One broadcast at a time. The message is copied, so it arrives without a
    "forwarded from" header and without revealing the admin who sent it, unless
    the admin asks to forward a message they forwarded to the bot: then it
    keeps that message's original header."""

    def __init__(
        self,
        client: Any,
        store: Store,
        *,
        pace_s: float = PACE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
        send_timeout_s: float = SEND_TIMEOUT_S,
        report_timeout_s: float = REPORT_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._store = store
        self._pace_s = pace_s
        self._sleep = sleep
        self._now = now
        self._send_timeout_s = send_timeout_s
        self._report_timeout_s = report_timeout_s
        self.running = False

    def claim(self) -> bool:
        """Take the one broadcast slot, before anything is awaited: two admins
        confirming at once must not both get as far as sending. False if it is
        taken. send() gives it back; so must a caller that never calls send()."""
        if self.running:
            return False
        self.running = True
        return True

    def release(self) -> None:
        self.running = False

    async def send(
        self,
        message: Any,
        report: Report,
        *,
        forward: bool = False,
        refetch: Refetch | None = None,
    ) -> Tally:
        """Send under the slot claim() took, and give it back when done.
        `refetch` fetches the draft again, for when its media's file reference
        expires partway through."""
        if not self.running:
            raise RuntimeError("claim() the broadcast first")
        try:
            return await self._send(_Run(message, forward, refetch, Tally(total=0)), report)
        finally:
            self.release()

    async def _send(self, run: _Run, report: Report) -> Tally:
        users = self._store.reachable_users()
        run.tally.total = len(users)
        last_report = self._now()
        for user_id in users:
            if not await self._deliver(user_id, run):
                run.tally.stopped = True
                break
            if self._now() - last_report >= REPORT_EVERY_S:
                last_report = self._now()
                await self._report(report, run.tally)
            await self._sleep(self._pace_s)
        return run.tally

    async def _report(self, report: Report, tally: Tally) -> None:
        try:
            async with asyncio.timeout(self._report_timeout_s):
                await report(tally)
        except TimeoutError:
            log.info("broadcast progress update timed out")

    async def _refetch(self, run: _Run) -> bool:
        if run.refetch is None:
            return False
        try:
            async with asyncio.timeout(self._send_timeout_s):
                fresh = await run.refetch()
        except Exception:
            log.warning("could not fetch the broadcast draft again", exc_info=True)
            return False
        if fresh is None:
            return False
        run.message = fresh
        return True

    async def _deliver(self, user_id: int, run: _Run) -> bool:
        """Count one delivery. False means stop the whole broadcast."""
        tally = run.tally
        refreshed = False
        for _ in range(3):
            try:
                async with asyncio.timeout(self._send_timeout_s):
                    if run.forward:
                        await self._client.forward_messages(user_id, run.message)
                    else:
                        await self._client.send_message(user_id, run.message)
            except FloodWaitError as exc:
                if exc.seconds > MAX_WAIT_S:
                    log.warning("broadcast stopped by a %s s FloodWait", exc.seconds)
                    return False
                await self._sleep(exc.seconds + 1)
                continue
            except FileReferenceExpiredError:
                # A long broadcast can outlive the file reference of its media.
                # The draft fetched again carries a fresh one, kept for everyone
                # after. A draft that is gone, or still refused, ends it.
                if refreshed or not await self._refetch(run):
                    log.warning("broadcast stopped: the draft's media is no longer available")
                    return False
                refreshed = True
                continue
            except _UNREACHABLE:
                self._store.mark_blocked(user_id)
                tally.unreachable += 1
                return True
            except RPCError as exc:
                log.info("broadcast to %s failed: %s", user_id, exc)
                tally.failed += 1
                return True
            except (ValueError, TimeoutError) as exc:
                # ValueError is Telethon unable to resolve this person, or out
                # of retries for Telegram's own errors. A timeout is not sent
                # again: the message may have arrived.
                log.info("broadcast to %s failed: %r", user_id, exc)
                tally.failed += 1
                return True
            tally.sent += 1
            return True
        tally.failed += 1
        return True
