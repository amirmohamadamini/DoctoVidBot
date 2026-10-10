from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import ChatAdminRequiredError, FloodWaitError, UserNotParticipantError
from telethon.tl.types import ChannelParticipant, ChannelParticipantLeft, PeerUser

from doctovid.membership import MEMBER_TTL_S, CheckFailed, Membership

from .conftest import EN

MEMBER = SimpleNamespace(participant=ChannelParticipant(user_id=1, date=None))
LEFT = SimpleNamespace(participant=ChannelParticipantLeft(peer=PeerUser(1)))


class FakeClient:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked = 0

    async def __call__(self, request: Any) -> Any:
        self.asked += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_me(self, input_peer: bool = False) -> Any:
        return "me"


async def test_a_member_is_let_in_and_remembered() -> None:
    clock = [0.0]
    client = FakeClient(MEMBER, MEMBER)
    membership = Membership(client, "news", now=lambda: clock[0])

    assert await membership.is_member(1)
    assert await membership.is_member(1)
    assert client.asked == 1

    clock[0] = MEMBER_TTL_S + 1
    assert await membership.is_member(1)
    assert client.asked == 2


async def test_non_members_are_asked_about_every_time() -> None:
    client = FakeClient(UserNotParticipantError(request=None), LEFT)
    membership = Membership(client, "news")
    assert not await membership.is_member(1)
    assert not await membership.is_member(1)
    assert client.asked == 2


async def test_a_broken_setup_lets_people_in_and_says_so() -> None:
    membership = Membership(FakeClient(ChatAdminRequiredError(request=None), MEMBER), "news")
    assert await membership.is_member(1)
    assert membership.broken
    assert await membership.is_member(2)
    assert not membership.broken


@pytest.mark.parametrize(
    "error", [FloodWaitError(request=None, capture=120), ValueError("Could not find the input")]
)
async def test_any_other_failure_lets_nobody_in(error: Exception) -> None:
    membership = Membership(FakeClient(error), "news")
    with pytest.raises(CheckFailed):
        await membership.is_member(1)
    assert not membership.broken


async def test_expired_confirmations_are_forgotten() -> None:
    clock = [0.0]
    membership = Membership(FakeClient(MEMBER, MEMBER), "news", now=lambda: clock[0])
    await membership.is_member(1)
    clock[0] = MEMBER_TTL_S / 2
    await membership.is_member(2)
    clock[0] = MEMBER_TTL_S + 1
    membership.forget_expired()
    assert list(membership._confirmed) == [2]


async def test_problem_reports_a_bot_that_is_not_an_admin() -> None:
    membership = Membership(FakeClient(ChatAdminRequiredError(request=None)), "news")
    problem = await membership.problem(EN)
    assert problem is not None
    assert "only admins can see who is a member" in problem
    assert await Membership(FakeClient(MEMBER), "news").problem(EN) is None
