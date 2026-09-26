"""The optional rule that users must join a channel before using the bot."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantBanned, ChannelParticipantLeft

log = logging.getLogger(__name__)

#: How long a "yes, they're a member" answer is trusted. Asking Telegram on
#: every file would be one extra request per file; not asking again at all
#: would let someone join, get in, and leave.
MEMBER_TTL_S = 600.0


#: What to tell an admin when the bot is in a channel without the right it needs.
POSTING_FIX = "Give the bot the right to post messages there."
MEMBERS_FIX = "Make the bot an admin there: only admins can see who is a member."


def channel_problem(exc: BaseException, *, fix: str) -> str:
    """What an admin should do about a channel the bot can't use, in words that
    fit a Telegram pop-up.

    "Could not find the input entity" (a ValueError from Telethon) usually
    means the bot has never seen the channel in this session: Telethon needs
    the channel's access hash, and learns it from an update. A post in the
    channel is such an update. It also covers a wrong id and a bot that isn't
    in the channel, so the advice names all three.
    """
    if isinstance(exc, ValueError | ChannelInvalidError | ChannelPrivateError):
        return (
            "I can't find that channel. Check the bot is an admin there, then post any "
            "message in the channel so I see it, and try again. Still failing? Check the id."
        )
    if isinstance(exc, ChatAdminRequiredError | ChatWriteForbiddenError):
        return f"I'm in that channel, but not allowed to do this. {fix}"
    return f"Telegram refused: {exc}" if str(exc) else f"Telegram refused ({type(exc).__name__})"


class Membership:
    def __init__(
        self,
        client: Any,
        channel: int | str,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._channel = channel
        self._now = now
        self._confirmed: dict[int, float] = {}

    async def is_member(self, user_id: int) -> bool:
        confirmed_at = self._confirmed.get(user_id)
        if confirmed_at is not None and self._now() - confirmed_at < MEMBER_TTL_S:
            return True
        try:
            member = await self._ask(user_id)
        except Exception:
            # The bot can't see the channel's members: it isn't an admin
            # there, or the channel is gone. Locking every user out over a
            # setup mistake would be worse than letting them in.
            log.warning("could not check channel membership, letting user in", exc_info=True)
            return True
        if member:
            self._confirmed[user_id] = self._now()
        else:
            self._confirmed.pop(user_id, None)
        return member

    async def problem(self) -> str | None:
        """Why the check can't work, or None. Asked about the bot itself, which
        is a member of the channel if it is set up correctly."""
        try:
            me = await self._client.get_me(input_peer=True)
            if not await self._ask(me):
                return "The bot isn't in that channel. Add it as an admin, then try again."
        except Exception as exc:
            return channel_problem(exc, fix=MEMBERS_FIX)
        return None

    async def _ask(self, user: Any) -> bool:
        try:
            result = await self._client(GetParticipantRequest(self._channel, user))
        except UserNotParticipantError:
            return False
        return not isinstance(result.participant, ChannelParticipantBanned | ChannelParticipantLeft)
